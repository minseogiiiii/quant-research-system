"""B1/B2/B3 contracts, entirely synthetic."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from q293_epoch_audit import TradeIndex, scan
from q293_long_policy import admission, budgets, process_sample, remaining
from q293_source_ledger import SourceLedger, audit


def wire_line(cid, seq, channel, events):
    utc = "2026-01-01T00:00:00Z"
    raw = json.dumps({"sequence_num": seq, "channel": channel,
                      "timestamp": utc, "events": events})
    return (json.dumps({"connection_id": cid, "raw_text": raw,
                        "received_utc": utc, "local_monotonic_ns": str(seq + 1),
                        "sha256": hashlib.sha256(raw.encode()).hexdigest()})
            + "\n").encode()


def fixture(root):
    chunks = root / "chunks"
    chunks.mkdir()
    utc = "2026-01-01T00:00:00Z"
    entries = []
    for cid in (0, 1):
        rows = []
        for kind in ("snapshot", "update"):
            events = [{"product_id": p, "type": kind, "updates": [
                {"side": side, "price_level": px, "new_quantity": "1",
                 "event_time": utc} for side, px in (("bid", "10"), ("offer", "11"))
            ]} for p in ("BTC-USD", "ETH-USD")]
            rows.append(wire_line(cid, len(rows), "l2_data", events))
        rows.append(wire_line(cid, 2, "heartbeats", []))
        raw = b"".join(rows)
        name = f"chunk_{cid:05d}_conn_{cid:03d}.jsonl"
        (chunks / name).write_bytes(raw)
        entries.append({"file": "chunks/" + name, "connection_id": cid,
                        "archive_sha256": hashlib.sha256(raw).hexdigest(),
                        "messages": len(rows), "raw_text_bytes": sum(
                            len(json.loads(row)["raw_text"].encode()) for row in rows),
                        "archived_line_bytes": len(raw), "first_sequence": 0,
                        "last_sequence": 2, "first_received_utc": utc,
                        "last_received_utc": utc})
    (chunks / "chunk_00002_conn_001.jsonl.partial").write_bytes(b"not sealed")
    raw = json.dumps({"format": "btc_eth_raw_soak_v10",
                      "source": "wss://advanced-trade-ws.coinbase.com",
                      "products": ["BTC-USD", "ETH-USD"], "orders_enabled": False,
                      "executable_alpha_verified": False, "chunks": entries}).encode()
    (root / "manifest.json").write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


class LongContracts(unittest.TestCase):
    def test_budget_and_absolute_deadline(self):
        self.assertGreater(budgets(1800)[0], budgets(180)[0])
        self.assertGreater(admission(18000, 3), admission(18000))
        self.assertLess(remaining(time.monotonic() + 80, 1800), 31)
        self.assertLess(remaining(time.monotonic() + 10, 1800), 15)

    def test_thread_spawned_child_accounted(self):
        children = []
        thread = threading.Thread(target=lambda: children.append(
            subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])))
        thread.start()
        thread.join()
        try:
            sample = process_sample(os.getpid())
            self.assertTrue(any(pid == children[0].pid
                                for pid, _ in sample["identity"]))
            self.assertGreaterEqual(sample["process_count"], 2)
            self.assertGreater(sample["rss_bytes"], 0)
        finally:
            children[0].terminate()
            children[0].wait(timeout=3)

    def test_two_sealed_epochs_partial_never_promoted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pin = fixture(root)
            result = scan(root, pin, "primary")
            self.assertEqual(result["sealed_messages"], 6)
            self.assertEqual(len(result["epochs"]), 2)
            self.assertEqual(result["unsealed_entries_excluded"], 1)
            self.assertFalse(result["zero_gap_certified"])
            self.assertTrue(all(not e["complete_epoch_certified"]
                                for e in result["epochs"]))
            path = root / "chunks/chunk_00000_conn_000.jsonl"
            path.write_bytes(path.read_bytes() + b"broken")
            with self.assertRaises(ValueError):
                scan(root, pin, "primary")

    def test_cross_source_trade_join_and_conflict(self):
        with tempfile.TemporaryDirectory() as folder:
            index = TradeIndex(Path(folder) / "trade.db")
            trade = {"product_id": "BTC-USD", "trade_id": "42", "price": "1.00",
                     "size": "2", "side": "BUY", "time": "2026-01-01T00:00:00Z"}
            try:
                index.add("primary", trade)
                index.add("secondary", {**trade, "price": "1"})
                result = index.compare()["BTC-USD"]
                self.assertEqual(result["matching_ids"], 1)
                self.assertFalse(result["l2_equivalence_certified"])
                with self.assertRaises(ValueError):
                    index.add("primary", {**trade, "size": "3"})
                index.add("primary", {**trade, "trade_id": "43"})
                index.add("secondary", {**trade, "trade_id": "43", "size": "3"})
                self.assertEqual(index.compare()["BTC-USD"]["divergent_ids"], 1)
            finally:
                index.close()

    def test_source_ledger_standby_loss_and_disk_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.jsonl"
            ledger = SourceLedger(path)
            try:
                ledger.record("primary", 1, "READY", "a" * 32)
                ledger.record("secondary", 1, "READY", "b" * 32)
                ledger.record("secondary", 1, "NOT_READY")
                ledger.record("secondary", 2, "READY", "c" * 32)
                with patch("q293_source_ledger.os.fsync", side_effect=OSError):
                    with self.assertRaises(OSError):
                        ledger.record("secondary", 2, "RETIRED")
                with self.assertRaises(ValueError):
                    ledger.record("primary", 1, "STOPPED")
            finally:
                ledger.close()
            result = audit(path)
            self.assertEqual(result["states"]["secondary"]["NOT_READY"], 1)
            self.assertEqual(result["attempts"]["secondary"], 2)
            self.assertFalse(result["source_continuity_certified"])


if __name__ == "__main__":
    unittest.main()
