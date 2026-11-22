"""Real-record fixture plus adversarial, fully rehashed event-stream corruptions.

A captured checksum is not an authenticated signature: tests deliberately recompute
all hashes to check that semantic validation, not SHA comparison, blocks bad data.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "backend"))
def _q14_load_after_path_setup_0():
    from audit_l2_deep import StrictL2Error, deep_replay
    from audit_l2_pilot import L2AuditError, audit_session
    return StrictL2Error, deep_replay, L2AuditError, audit_session

StrictL2Error, deep_replay, L2AuditError, audit_session = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0

FIXTURE = BASE / "backend" / "data" / "l2_pilot_20261113_205058UTC"
SECOND = BASE / "backend" / "data" / "l2_pilot_second_35s_20261113"


def corrupt_semantics(root, select, change):
    """Modify one original message, then make every content hash consistent."""
    source = (root / "raw_websocket_payloads.jsonl").read_bytes().splitlines()
    lines = []
    stream = hashlib.sha256()
    found = False
    for line in source:
        item = json.loads(line)
        m = json.loads(item["raw_text"])
        if not found and select(m):
            change(m)
            payload = json.dumps(m, separators=(",", ":"), ensure_ascii=False)
            item["raw_text"] = payload
            item["sha256"] = hashlib.sha256(payload.encode()).hexdigest()
            found = True
        else:
            payload = item["raw_text"]
        stream.update(payload.encode() + b"\n")
        lines.append(
            (
                json.dumps(item, separators=(",", ":"), ensure_ascii=False) + "\n"
            ).encode()
        )
    if not found:
        raise AssertionError("Requested fixture message not found")
    (root / "raw_websocket_payloads.jsonl").write_bytes(b"".join(lines))
    status = json.loads((root / "capture_status.json").read_text())
    status["stream_hash"] = stream.hexdigest()
    status["payload_bytes"] = sum(
        len(json.loads(line)["raw_text"].encode()) for line in lines
    )
    (root / "capture_status.json").write_text(json.dumps(status, indent=2) + "\n")


class TestV8DeepReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clean = deep_replay(FIXTURE)

    def test_real_source_replays_exactly_and_is_not_alpha(self):
        r = self.clean
        self.assertEqual(r["total_messages"], 885)
        self.assertEqual(
            (r["connection_sequence_first"], r["connection_sequence_last"]), (0, 884)
        )
        self.assertEqual(r["heartbeat_events"], 20)
        self.assertEqual(r["per_product"]["BTC-USD"]["incremental_trade_records"], 222)
        self.assertEqual(r["per_product"]["ETH-USD"]["incremental_trade_records"], 80)
        self.assertFalse(r["independent_exchange_reference_book_verified"])
        self.assertFalse(r["strategy_alpha_verified"])
        self.assertEqual(r["per_product"]["BTC-USD"]["best_bid_final"], "68931.04")
        self.assertEqual(r["per_product"]["ETH-USD"]["best_ask_final"], "1949.69")

    def test_independent_replayers_agree_on_final_quotes_and_events(self):
        old = audit_session(FIXTURE)
        new = self.clean
        self.assertEqual(old["source_text_stream_sha256"], new["input_stream_sha256"])
        for product in ("BTC-USD", "ETH-USD"):
            oldbook = old["per_product"][product]
            newbook = new["per_product"][product]
            self.assertEqual(oldbook["best_bid_final"], newbook["best_bid_final"])
            self.assertEqual(oldbook["best_ask_final"], newbook["best_ask_final"])
            self.assertEqual(
                oldbook["individual_trades_reported"],
                newbook["source_reported_trades_total"],
            )

    def test_second_independently_recorded_session(self):
        r = deep_replay(SECOND)
        self.assertEqual(r["total_messages"], 1507)
        self.assertEqual(
            (r["connection_sequence_first"], r["connection_sequence_last"]), (0, 1506)
        )
        self.assertEqual(r["heartbeat_events"], 34)
        self.assertEqual(r["per_product"]["BTC-USD"]["incremental_trade_records"], 682)
        self.assertEqual(r["per_product"]["ETH-USD"]["incremental_trade_records"], 131)
        for p in ("BTC-USD", "ETH-USD"):
            self.assertFalse(r["collector_telemetry"][p]["capture_telemetry_mismatch"])
        old = audit_session(SECOND)
        self.assertEqual(old["source_text_stream_sha256"], r["input_stream_sha256"])
        self.assertFalse(r["independent_exchange_reference_book_verified"])

    def test_trades_not_assumed_chronologically_sorted(self):
        self.assertTrue(
            self.clean["per_product"]["BTC-USD"][
                "trade_messages_may_not_be_sorted_by_trade_time"
            ]
        )
        self.assertGreater(
            self.clean["per_product"]["BTC-USD"][
                "trade_time_regressions_in_message_order"
            ],
            0,
        )

    def test_reject_declared_transport_errors(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for filename in ("capture_status.json", "raw_websocket_payloads.jsonl"):
                shutil.copyfile(FIXTURE / filename, root / filename)
            s = json.loads((root / "capture_status.json").read_text())
            s["error_count"] = 1
            (root / "capture_status.json").write_text(json.dumps(s) + "\n")
            with self.assertRaises(StrictL2Error):
                deep_replay(root)

    def test_future_source_envelope_time_rejected(self):
        self.run_tamper(
            lambda m: m["sequence_num"] == 30,
            lambda m: m.__setitem__("timestamp", "2030-01-01T00:00:00Z"),
        )

    def test_original_collector_trade_telemetry_is_known_incorrect(self):
        for p in ("BTC-USD", "ETH-USD"):
            self.assertTrue(
                self.clean["collector_telemetry"][p]["capture_telemetry_mismatch"]
            )
            self.assertEqual(
                self.clean["collector_telemetry"][p]["producer_reported_trades"], 0
            )

    def run_tamper(self, select, change, error_type=StrictL2Error, also_legacy=False):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ("capture_status.json", "raw_websocket_payloads.jsonl"):
                shutil.copyfile(FIXTURE / name, root / name)
            corrupt_semantics(root, select, change)
            with self.assertRaises(error_type):
                deep_replay(root)
            if also_legacy:
                with self.assertRaises(L2AuditError):
                    audit_session(root)

    def test_gapped_sequence_rejected_even_when_all_hashes_are_fixed(self):
        self.run_tamper(
            lambda m: m["sequence_num"] == 30,
            lambda m: m.__setitem__("sequence_num", 999),
            also_legacy=True,
        )

    def test_duplicated_trade_id_rejected_with_valid_recomputed_hashes(self):
        def change(m):
            e = m["events"][0]
            e["trades"][1]["trade_id"] = e["trades"][0]["trade_id"]

        self.run_tamper(
            lambda m: (
                m.get("channel") == "market_trades"
                and any(len(e.get("trades", [])) > 1 for e in m["events"])
            ),
            change,
            also_legacy=True,
        )

    def test_skipped_heartbeat_detected(self):
        self.run_tamper(
            lambda m: (
                m["channel"] == "heartbeats"
                and m["events"][0]["heartbeat_counter"] % 10 == 5
            ),
            lambda m: m["events"][0].__setitem__(
                "heartbeat_counter", m["events"][0]["heartbeat_counter"] + 5
            ),
        )

    def test_crossed_book_rejected(self):
        def change(m):
            m["events"][0]["updates"][0].update(
                {"side": "bid", "price_level": "1000000", "new_quantity": "2"}
            )

        self.run_tamper(
            lambda m: (
                m["channel"] == "l2_data"
                and m["events"][0]["type"] == "update"
                and m["events"][0]["product_id"] == "BTC-USD"
                and m["events"][0]["updates"]
            ),
            change,
            also_legacy=True,
        )

    def test_missing_initial_snapshot_rejected(self):
        self.run_tamper(
            lambda m: (
                m["channel"] == "l2_data"
                and m["events"][0]["type"] == "snapshot"
                and m["events"][0]["product_id"] == "BTC-USD"
            ),
            lambda m: m["events"].clear(),
            also_legacy=True,
        )


if __name__ == "__main__":
    unittest.main()
