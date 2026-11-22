"""Q23 redacted fail-closed fault and bounded full-source replay regressions."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend"))

from q23_redacted_diagnostics import (  # noqa: E402
    failure_receipt,
    named_failures,
    redacted_snapshot,
)
from q23_isolated_replay_worker import (  # noqa: E402
    PRIVATE_STATE_ROWS_PER_PRODUCT,
    WORKER_AS_BYTES,
    replay_primary,
    verify_manifest_pin,
)
from q22_ci_full_replay import _book_agreement  # noqa: E402
from q22_ci_5h_supervisor import Q22Rejected, check_private_health  # noqa: E402

SESSION = "a" * 32
MS = 1_800_000_000_000


def healthy():
    return {
        "mode": "LIVE_SOURCE", "status": "HEALTHY", "live_feed": True,
        "operationally_healthy": True, "trading_enabled": False,
        "snapshots": ["BTC-USD", "ETH-USD"],
        "subscriptions": ["heartbeats", "level2", "market_trades"],
        "connection_id": 0, "connection_gaps": 0,
        "session_id": SESSION, "messages": 100, "last_sequence": 99,
        "heartbeats": 10, "last_receiver_time_ms": MS,
    }


def metrics():
    return {
        "max_sse_clients": 8,
        "active_sse_connections": 4,
        "monitor_transport": "UNIX_SOCKET",
        "trading_enabled": False,
        "execution_authorized": False,
    }


def _payload(seq: int, event: dict, timestamp: str) -> str:
    return json.dumps({
        "channel": "l2_data", "events": [event],
        "sequence_num": seq, "timestamp": timestamp,
    }, separators=(",", ":"))


def _large_private_fixture(root: Path, updates_each: int = 10_010):
    """Synthetic and NEVER LIVE; two products each >10k 1Hz book states."""
    (root / "chunks").mkdir()
    t0 = datetime(2026, 11, 1, tzinfo=UTC)
    blob_path = root / "chunks/chunk_00000_conn_000.jsonl"
    first = last = None
    raw_total = 0
    seq = 0
    digest = hashlib.sha256()
    count = 0
    # Interleaved updates span >5h, with every product sampled at ~1.8s.
    with blob_path.open("wb") as stream:
        for i in range(updates_each + 1):
            for product in ("BTC-USD", "ETH-USD"):
                when = t0 + timedelta(milliseconds=900 * seq)
                stamp = when.isoformat(timespec="milliseconds").replace("+00:00", "Z")
                updates = [
                    {
                        "side": "bid",
                        "price_level": "70000" if product == "BTC-USD" else "2000",
                        "new_quantity": "2" if i % 2 else "1",
                        "event_time": stamp,
                    },
                    {
                        "side": "offer",
                        "price_level": "70001" if product == "BTC-USD" else "2001",
                        "new_quantity": "2",
                        "event_time": stamp,
                    },
                ]
                event = {
                    "type": "snapshot" if i == 0 else "update",
                    "product_id": product,
                    "updates": updates,
                }
                raw = _payload(seq, event, stamp)
                entry = {
                    "connection_id": 0,
                    "received_utc": stamp,
                    "local_monotonic_ns": str(1_000_000_000 + seq * 900_000_000),
                    "raw_text": raw,
                    "sha256": hashlib.sha256(raw.encode()).hexdigest(),
                }
                line = (json.dumps(entry, separators=(",", ":")) + "\n").encode()
                stream.write(line)
                digest.update(line)
                raw_total += len(raw.encode())
                first = stamp if first is None else first
                last = stamp
                seq += 1
                count += len(line)
    meta = {
        "format": "btc_eth_raw_soak_v10",
        "source": "wss://advanced-trade-ws.coinbase.com",
        "source_authentication": "PUBLIC_WEBSOCKET_NO_SIGNED_EXCHANGE_ATTESTATION",
        "products": ["BTC-USD", "ETH-USD"],
        "sequence_scope": "PER_WEBSOCKET_CONNECTION",
        "orders_enabled": False,
        "strategy_execution_enabled": False,
        "point_in_time_historical_availability_verified": False,
        "executable_alpha_verified": False,
        "synthetic_fixture": True,
        "ready_for_research": True,
        "status": "CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH",
        "complete_sequence_per_connection": True,
        "all_connections_have_snapshots": True,
        "faults": [],
        "chunks": [{
            "file": "chunks/chunk_00000_conn_000.jsonl",
            "connection_id": 0,
            "messages": seq,
            "archived_line_bytes": count,
            "raw_text_bytes": raw_total,
            "archive_sha256": digest.hexdigest(),
            "first_sequence": 0, "last_sequence": seq - 1,
            "first_received_utc": first, "last_received_utc": last,
        }],
        "connections": [{
            "id": 0, "messages": seq, "last_sequence": seq - 1,
            "snapshots": {"BTC-USD": True, "ETH-USD": True},
            "updates": {"BTC-USD": updates_each, "ETH-USD": updates_each},
            "heartbeat_count": 0, "integrity_fault": False,
        }],
        "messages": seq,
        "bytes": raw_total,
    }
    manifest = (json.dumps(meta, sort_keys=True) + "\n").encode()
    (root / "manifest.json").write_bytes(manifest)
    return hashlib.sha256(manifest).hexdigest()


class Q23Diagnostics(unittest.TestCase):
    def test_healthy_source_has_no_faults(self):
        self.assertEqual(named_failures(healthy(), metrics(), now_ms=MS + 100), [])
        with patch("q22_ci_5h_supervisor.time.time_ns",
                   return_value=(MS + 100) * 1_000_000):
            row = check_private_health(healthy(), metrics(), None)
        self.assertEqual(row["messages"], 100)

    def test_operator_503_is_fault_not_healthy(self):
        h = healthy()
        h.update(status="STALE", operationally_healthy=False,
                 reason="HEARTBEAT_STALE")
        codes = named_failures(h, metrics(), now_ms=MS + 25_000,
                               health_http=503)
        self.assertIn("OPERATOR_HEALTH_HTTP_503", codes)
        self.assertIn("SOURCE_NOT_HEALTHY", codes)
        self.assertIn("RECEIVER_STALE", codes)
        with patch("q22_ci_5h_supervisor.time.time_ns",
                   return_value=(MS + 25_000) * 1_000_000), self.assertRaises(
                       Q22Rejected
                   ):
            check_private_health(h, metrics(), None, health_http=503)

    def test_forced_disconnection_and_resession_are_rejected(self):
        h = healthy()
        h.update(
            connection_id=1, connection_gaps=1,
            session_id="b" * 32,
            status="RECONNECTING",
            operationally_healthy=False,
            reason="COLLECTOR_REPORTED_WEBSOCKET_SOURCE_GAP",
        )
        previous = {
            "session_hash": hashlib.sha256(SESSION.encode()).hexdigest(),
            "messages": 90, "heartbeats": 8,
        }
        codes = named_failures(h, metrics(), previous, now_ms=MS + 200,
                               health_http=503)
        for code in (
            "SOURCE_CONNECTION_GAP", "CONNECTION_ID_CHANGED",
            "SOURCE_SESSION_CHANGED", "SOURCE_NOT_HEALTHY",
        ):
            self.assertIn(code, codes)

    def test_receipt_contains_no_raw_ids_prices_or_untrusted_reason(self):
        h = healthy()
        h.update(reason="SOURCE_ENVELOPE_REJECTED:price=999999 secret123",
                 status="QUARANTINED", operationally_healthy=False)
        h["raw_text"] = "leaked raw market frame 777777"
        h["api_key"] = "SECRET"
        obj = failure_receipt(
            h, metrics(), ["SOURCE_NOT_HEALTHY"], [],
            100, 0, 503,
        )
        body = json.dumps(obj, sort_keys=True)
        for forbidden in ("SECRET", "secret123", SESSION,
                          "777777", "999999", "raw_text", "api_key"):
            self.assertNotIn(forbidden, body)
        self.assertEqual(
            obj["last_private_status"]["source_reason_class"],
            "SOURCE_ENVELOPE_REJECTED",
        )
        self.assertEqual(obj["recent_samples"], [])

    def test_counter_regression_produces_named_failure(self):
        h = healthy()
        previous = {
            "session_hash": hashlib.sha256(SESSION.encode()).hexdigest(),
            "messages": 100, "heartbeats": 11,
        }
        codes = named_failures(h, metrics(), previous, now_ms=MS + 100)
        self.assertIn("NO_MESSAGES_SINCE_LAST_SAMPLE", codes)
        self.assertIn("HEARTBEAT_REGRESSION", codes)


class Q23Replay(unittest.TestCase):
    def test_bounded_worker_contract(self):
        self.assertEqual(PRIVATE_STATE_ROWS_PER_PRODUCT, 30_000)
        self.assertEqual(WORKER_AS_BYTES, 3 * 1024**3)

    def test_full_archive_more_than_10000_states_each_product(self):
        # Expensive deterministic stress. Limit > 10k must be real replay,
        # never an isolated constant-only assertion.
        with tempfile.TemporaryDirectory(prefix="q23_long_fixture_") as temp:
            source = Path(temp) / "source"
            source.mkdir()
            pin = _large_private_fixture(source)
            verify_manifest_pin(source, pin)
            import v35_archive_ingress as ingress
            import v35_replay_service as primary

            with self.assertRaisesRegex(
                primary.StateRejected, "BOUNDED_STATE_HISTORY_EXCEEDED"
            ):
                primary.replay_capture(source, pin)
            with patch.object(primary, "MAX_ROWS_PER_PRODUCT",
                              PRIVATE_STATE_ROWS_PER_PRODUCT):
                result = primary.replay_capture(source, pin)
            self.assertEqual(result.message_count, 2 * 10011)
            for product in ("BTC-USD", "ETH-USD"):
                self.assertGreater(len(result.rows[product]), 10000)
                self.assertLessEqual(
                    len(result.rows[product]), PRIVATE_STATE_ROWS_PER_PRODUCT
                )
            # Valid source is synthetic and not promoted to LIVE or real data.
            self.assertTrue(
                json.loads((source / "manifest.json").read_bytes())[
                    "synthetic_fixture"
                ]
            )

    def test_comparison_refuses_divergent_final_books(self):
        p = {
            "messages": 7,
            "products": {
                k: {"best_bid": "10", "best_ask": "11",
                    "bid_levels": 3, "ask_levels": 4,
                    "retained_states": 12}
                for k in ("BTC-USD", "ETH-USD")
            },
        }
        ind = {
            "messages": 7,
            "products": {
                k: {"best_bid": "10", "best_ask": "11",
                    "bid_levels": 3, "ask_levels": 4}
                for k in ("BTC-USD", "ETH-USD")
            },
        }
        _book_agreement(p, ind)
        ind["products"]["BTC-USD"]["best_ask"] = "12"
        from q22_ci_full_replay import FiveHourReplayRejected
        with self.assertRaises(FiveHourReplayRejected):
            _book_agreement(p, ind)


if __name__ == "__main__":
    unittest.main()
