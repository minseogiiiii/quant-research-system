"""v10 deterministic poison-pill tests for source archive and admission rules.

All market events are synthetic fixtures, never labeled historical market data.
"""

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from audit_soak import SoakAuditError, audit

UTC = UTC


def stamp(t):
    return t.isoformat().replace("+00:00", "Z")


def market_event(i, t):
    common = {"sequence_num": i, "timestamp": stamp(t)}
    if i in (1, 2):
        product = "BTC-USD" if i == 1 else "ETH-USD"
        price = "100" if i == 1 else "200"
        ask = "101" if i == 1 else "202"
        return {
            **common,
            "channel": "l2_data",
            "events": [
                {
                    "type": "snapshot",
                    "product_id": product,
                    "updates": [
                        {
                            "side": "bid",
                            "price_level": price,
                            "new_quantity": "3",
                            "event_time": stamp(t),
                        },
                        {
                            "side": "offer",
                            "price_level": ask,
                            "new_quantity": "2",
                            "event_time": stamp(t),
                        },
                    ],
                }
            ],
        }
    if i in (3, 4):
        product = "BTC-USD" if i == 3 else "ETH-USD"
        price = "100" if i == 3 else "202"
        side = "bid" if i == 3 else "offer"
        return {
            **common,
            "channel": "l2_data",
            "events": [
                {
                    "type": "update",
                    "product_id": product,
                    "updates": [
                        {
                            "side": side,
                            "price_level": price,
                            "new_quantity": "4",
                            "event_time": stamp(t),
                        }
                    ],
                }
            ],
        }
    if i == 5:
        return {
            **common,
            "channel": "heartbeats",
            "events": [{"heartbeat_counter": "1", "current_time": stamp(t)}],
        }
    if i == 6:
        return {
            **common,
            "channel": "market_trades",
            "events": [
                {
                    "type": "update",
                    "trades": [
                        {
                            "product_id": "BTC-USD",
                            "price": "100.5",
                            "size": "0.02",
                            "trade_id": "t1",
                            "side": "BUY",
                            "time": stamp(t),
                        }
                    ],
                }
            ],
        }
    return {**common, "channel": "subscriptions", "events": []}


def create_archive(
    root, mutate=None, status="CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH"
):
    root.mkdir(parents=True)
    (root / "chunks").mkdir()
    now = datetime.now(UTC).replace(microsecond=0)
    events = [market_event(i, now) for i in range(7)]
    if mutate:
        mutate(events)
    raw_rows = []
    for i, event in enumerate(events):
        raw = json.dumps(event, separators=(",", ":"))
        raw_rows.append(
            json.dumps(
                {
                    "connection_id": 0,
                    "received_utc": stamp(now),
                    "local_monotonic_ns": str(10_000_000 + i),
                    "raw_text": raw,
                    "sha256": hashlib.sha256(raw.encode()).hexdigest(),
                },
                separators=(",", ":"),
            )
            + "\n"
        )
    half = 3
    chunks = []
    for ix, sl in enumerate((raw_rows[:half], raw_rows[half:])):
        filename = f"chunk_{ix:05d}_conn_000.jsonl"
        data = "".join(sl).encode()
        (root / "chunks" / filename).write_bytes(data)
        lo, hi = (0, 2) if ix == 0 else (3, 6)
        chunks.append(
            {
                "file": "chunks/" + filename,
                "connection_id": 0,
                "messages": len(sl),
                "raw_text_bytes": sum(
                    len(json.loads(x)["raw_text"].encode()) for x in sl
                ),
                "archived_line_bytes": len(data),
                "first_sequence": events[lo]["sequence_num"],
                "last_sequence": events[hi]["sequence_num"],
                "first_received_utc": stamp(now),
                "last_received_utc": stamp(now),
                "archive_sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    total_raw = sum(c["raw_text_bytes"] for c in chunks)
    manifest = {
        "format": "btc_eth_raw_soak_v10",
        "source": "wss://advanced-trade-ws.coinbase.com",
        "source_authentication": "PUBLIC_WEBSOCKET_NO_SIGNED_EXCHANGE_ATTESTATION",
        "products": ["BTC-USD", "ETH-USD"],
        "subscribed_channels": ["level2", "market_trades", "heartbeats"],
        "sampled_from_utc": stamp(now - timedelta(seconds=1)),
        "finished_at_utc": stamp(now + timedelta(seconds=10)),
        "required_duration_seconds": 10,
        "status": status,
        "orders_enabled": False,
        "strategy_execution_enabled": False,
        "point_in_time_historical_availability_verified": False,
        "executable_alpha_verified": False,
        "sequence_scope": "PER_WEBSOCKET_CONNECTION",
        "chunks": chunks,
        "connections": [
            {
                "id": 0,
                "state": "DISCONNECTED",
                "started_utc": stamp(now),
                "ended_utc": stamp(now + timedelta(seconds=10)),
                "messages": 7,
                "last_sequence": events[-1]["sequence_num"],
                "snapshots": {"BTC-USD": True, "ETH-USD": True},
                "updates": {"BTC-USD": 1, "ETH-USD": 1},
                "became_ready": True,
                "heartbeat_count": 1,
                "integrity_fault": False,
            }
        ],
        "faults": [],
        "bytes": total_raw,
        "messages": 7,
        "ready_for_research": status
        == "CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH",
        "complete_sequence_per_connection": True,
        "all_connections_have_snapshots": True,
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def reseal_chunk(root, index):
    mf = json.loads((root / "manifest.json").read_text())
    c = mf["chunks"][index]
    p = root / c["file"]
    raw = p.read_bytes()
    c["archive_sha256"] = hashlib.sha256(raw).hexdigest()
    c["archived_line_bytes"] = len(raw)
    (root / "manifest.json").write_text(json.dumps(mf))


class V10SoakTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "sample"
        create_archive(self.root)

    def assertRejected(self):
        with self.assertRaises(SoakAuditError):
            audit(self.root)

    def test_valid_source_and_reproducible_summary(self):
        r = audit(self.root)
        self.assertEqual(r["status"], "INTERNALLY_REPLAYED_NO_KNOWN_GAP")
        self.assertEqual(r["message_count"], 7)
        self.assertEqual(r["chunk_count"], 2)
        self.assertEqual(r["connection_sequences"], {"0": [0, 6]})
        self.assertEqual(r["per_product"]["BTC-USD"]["unique_update_trade"], 1)
        self.assertFalse(r["independent_market_truth_verified"])
        self.assertFalse(r["orders_enabled"])

    def test_unlisted_or_partial_chunk_fails(self):
        (self.root / "chunks" / "orphan.jsonl.partial").write_text("bad")
        self.assertRejected()

    def test_source_byte_modified_fails(self):
        p = self.root / "chunks" / "chunk_00000_conn_000.jsonl"
        p.write_bytes(p.read_bytes().replace(b"BTC-USD", b"ETH-USD", 1))
        self.assertRejected()

    def test_sequence_gap_even_when_all_hashes_recomputed(self):
        p = self.root / "chunks" / "chunk_00001_conn_000.jsonl"
        rows = [json.loads(l) for l in p.read_text().splitlines()]
        msg = json.loads(rows[0]["raw_text"])
        msg["sequence_num"] += 5
        rows[0]["raw_text"] = json.dumps(msg)
        rows[0]["sha256"] = hashlib.sha256(rows[0]["raw_text"].encode()).hexdigest()
        p.write_text("".join(json.dumps(x) + "\n" for x in rows))
        reseal_chunk(self.root, 1)
        self.assertRejected()

    def test_absent_l2_snapshot_fails(self):
        p = self.root / "chunks" / "chunk_00000_conn_000.jsonl"
        rows = [json.loads(l) for l in p.read_text().splitlines()]
        msg = json.loads(rows[1]["raw_text"])
        msg["events"][0]["type"] = "update"
        rows[1]["raw_text"] = json.dumps(msg)
        rows[1]["sha256"] = hashlib.sha256(rows[1]["raw_text"].encode()).hexdigest()
        p.write_text("".join(json.dumps(x) + "\n" for x in rows))
        reseal_chunk(self.root, 0)
        self.assertRejected()

    def test_negative_quantity_even_when_rehashed_fails(self):
        p = self.root / "chunks" / "chunk_00001_conn_000.jsonl"
        rows = [json.loads(l) for l in p.read_text().splitlines()]
        msg = json.loads(rows[0]["raw_text"])
        msg["events"][0]["updates"][0]["new_quantity"] = "-8"
        rows[0]["raw_text"] = json.dumps(msg)
        rows[0]["sha256"] = hashlib.sha256(rows[0]["raw_text"].encode()).hexdigest()
        p.write_text("".join(json.dumps(x) + "\n" for x in rows))
        reseal_chunk(self.root, 1)
        self.assertRejected()

    def test_manifest_forged_ready_without_data_fails(self):
        mf = json.loads((self.root / "manifest.json").read_text())
        mf["connections"][0]["became_ready"] = False
        (self.root / "manifest.json").write_text(json.dumps(mf))
        self.assertRejected()

    def test_manifest_orders_enabled_fails(self):
        mf = json.loads((self.root / "manifest.json").read_text())
        mf["orders_enabled"] = True
        (self.root / "manifest.json").write_text(json.dumps(mf))
        self.assertRejected()

    def test_gapped_capture_cannot_be_admitted(self):
        mf = json.loads((self.root / "manifest.json").read_text())
        mf["status"] = "CAPTURE_COMPLETED_WITH_QUARANTINED_GAPS"
        mf["faults"] = [{"reason": "DISCONNECT", "connection_id": 0}]
        mf["ready_for_research"] = True
        (self.root / "manifest.json").write_text(json.dumps(mf))
        self.assertRejected()

    def test_partial_capture_readable_but_not_admitted(self):
        mf = json.loads((self.root / "manifest.json").read_text())
        mf["status"] = "CAPTURE_INCOMPLETE_OR_INVALID"
        mf["ready_for_research"] = False
        (self.root / "manifest.json").write_text(json.dumps(mf))
        r = audit(self.root)
        self.assertEqual(r["status"], "ARCHIVED_BUT_NOT_ADMITTED")
        self.assertFalse(r["eligible_for_point_in_time_feature_research"])

    def test_symlink_chunk_rejected(self):
        f = self.root / "chunks" / "chunk_00000_conn_000.jsonl"
        target = self.root / "copy"
        target.write_bytes(f.read_bytes())
        f.unlink()
        f.symlink_to(target)
        self.assertRejected()

    def test_locked_and_crossed_book_rejected(self):
        p = self.root / "chunks" / "chunk_00001_conn_000.jsonl"
        rows = [json.loads(l) for l in p.read_text().splitlines()]
        msg = json.loads(rows[0]["raw_text"])
        msg["events"][0]["updates"][0]["price_level"] = "102"
        rows[0]["raw_text"] = json.dumps(msg)
        rows[0]["sha256"] = hashlib.sha256(rows[0]["raw_text"].encode()).hexdigest()
        p.write_text("".join(json.dumps(x) + "\n" for x in rows))
        reseal_chunk(self.root, 1)
        self.assertRejected()

    def test_truncated_chunk_line_rejected(self):
        p = self.root / "chunks" / "chunk_00001_conn_000.jsonl"
        p.write_bytes(p.read_bytes().rstrip(b"\n"))
        reseal_chunk(self.root, 1)
        self.assertRejected()

    def test_utc_timestamp_required(self):
        mf = json.loads((self.root / "manifest.json").read_text())
        mf["sampled_from_utc"] = "2026-11-13T20:00:00"
        (self.root / "manifest.json").write_text(json.dumps(mf))
        self.assertRejected()

    def test_no_future_historical_availability_claim(self):
        mf = json.loads((self.root / "manifest.json").read_text())
        mf["point_in_time_historical_availability_verified"] = True
        (self.root / "manifest.json").write_text(json.dumps(mf))
        self.assertRejected()


if __name__ == "__main__":
    unittest.main()
