"""Regression for actual B4 evidence loss; no external data."""
import json
import tempfile
import unittest
from pathlib import Path

from q293_b4_diagnostics import (
    ObserverJournal,
    fault_class,
    manifest_receipt,
    observer_receipt,
)


class DiagnosticContracts(unittest.TestCase):
    def test_manifest_fault_order_and_redaction(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "manifest.json"
            path.write_text(json.dumps({
                "format": "btc_eth_raw_soak_v10", "orders_enabled": False,
                "connections": [{}, {}], "finish_reason": "SIGTERM_QUARANTINE",
                "faults": [{"connection_id": 0, "reason": "SOURCE:Error: SEQUENCE_GAP"},
                           {"connection_id": 1, "reason": "ERROR: private data"}],
            }))
            receipt = manifest_receipt(path)
            self.assertEqual(receipt["first_fault_class"], "SEQUENCE_GAP")
            self.assertEqual(receipt["faults"][1]["class"], "ERROR")
            self.assertNotIn("private data", json.dumps(receipt))
            self.assertFalse(receipt["network_root_cause_proven"])
            self.assertEqual(fault_class("SOURCE:Error: SIZE_CAP"), "SIZE_CAP")
            self.assertEqual(fault_class("SOURCE:Error: SEQUENCE_GAP malicious"),
                             "UNKNOWN_REDACTED")

    def test_observer_first_event_survives_later_shutdown(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            journal = ObserverJournal(path)
            try:
                journal.record("READY", 0, 10)
                journal.record("COLLECTOR_REPORTED_WEBSOCKET_SOURCE_GAP", 0, 11)
                journal.record("SOURCE_STOPPED", 0, 11)
            finally:
                journal.close()
            receipt = observer_receipt(path)
            self.assertEqual(receipt["status"], "VERIFIED")
            self.assertEqual(receipt["events"][1]["reason"],
                             "COLLECTOR_REPORTED_WEBSOCKET_SOURCE_GAP")
            path.write_bytes(path.read_bytes().replace(b"READY", b"STALE"))
            self.assertEqual(observer_receipt(path)["status"],
                             "UNAVAILABLE_OR_REJECTED")


if __name__ == "__main__":
    unittest.main()
