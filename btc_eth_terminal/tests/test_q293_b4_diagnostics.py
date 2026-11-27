"""Regression for actual B4 evidence loss; no external data."""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from q28_transport_incident_receipt import incident_receipt
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

    def test_duplicate_inside_first_packet_receipt_and_redaction(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "manifest.json"
            r = dict(schema="q294_dup_snapshot_v1", connection_id=0, sequence=0,
                     previous_committed_sequence=None, product="BTC-USD",
                     event_index=1, event_count=2, level_count=1,
                     already_committed_snapshot=False, prior_updates=0,
                     raw_sha256="a"*64, same_socket=True, root_cause_proven=False,
                     raw_text="private")
            m = dict(format="btc_eth_raw_soak_v10", orders_enabled=False,
                     connections=[dict(id=0, duplicate_snapshot_receipt=r)],
                     faults=[dict(connection_id=0,
                                  reason="SOURCE:Error: DUP_SNAPSHOT")])
            path.write_text(json.dumps(m))
            result = manifest_receipt(path)
            self.assertEqual(result["status"], "MANIFEST_READ")
            self.assertEqual(result["duplicate_snapshot_receipts"][0]["sequence"], 0)
            self.assertNotIn("private", json.dumps(result))
            r["sequence"] = 1
            path.write_text(json.dumps(m))
            self.assertEqual(manifest_receipt(path)["status"],
                             "MANIFEST_UNAVAILABLE_OR_REJECTED")

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


    def test_original_collector_local_sequence_fault_receipt(self):
        root = Path(__file__).resolve().parents[1]
        node = shutil.which("node")
        self.assertIsNotNone(node)
        with tempfile.TemporaryDirectory(prefix="q293fault_") as folder:
            work = Path(folder)
            env = {
                "PATH": os.environ["PATH"],
                "SOAK_TEST_FIXTURE": "1",
                "SOAK_DURATION_SECONDS": "30",
                "SOAK_RAW_BUDGET_BYTES": "300000000",
                "SOAK_ARCHIVE_BUDGET_BYTES": "500000000",
                "SOAK_DISK_RESERVE_BYTES": "0",
                "Q24_WS_DIAG_PRIVATE_PATH": str(work / "ws.jsonl"),
                "Q27_HEARTBEAT_GUARD_ENABLED": "1",
                "Q27_HEARTBEAT_TRACE_PRIVATE": str(work / "hb.jsonl"),
            }
            args = [node]
            for preload in (root / "tests/q293_sequence_fault_mock.mjs",
                            root / "scripts/q27_heartbeat_guard.mjs",
                            root / "scripts/q24_ws_close_probe.mjs"):
                args += ["--import", str(preload)]
            args.append(str(root / "scripts/btc_eth_multiday_v20.mjs"))
            child = subprocess.Popen(args, cwd=work, env=env,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 10
                manifest = None
                while time.monotonic() < deadline:
                    found = list(work.glob("soak_evidence/*/manifest.json"))
                    if len(found) == 1:
                        value = manifest_receipt(found[0])
                        if value.get("first_fault_class") == "SEQUENCE_GAP":
                            manifest = found[0]
                            break
                    self.assertIsNone(child.poll(), "collector exited prematurely")
                    time.sleep(.05)
                self.assertIsNotNone(manifest, "expected original collector fault")
                # Wait for the second socket; this is not proof of gap recovery.
                time.sleep(1)
            finally:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
            receipt = manifest_receipt(manifest)
            self.assertEqual(receipt["first_fault_class"], "SEQUENCE_GAP")
            result = incident_receipt(work / "ws.jsonl", work / "hb.jsonl")
            self.assertEqual(result["observed_termination_class"],
                             "LOCAL_NON_GUARD_CLOSE_OBSERVED")
            self.assertTrue(result["first_socket"]["second_socket_constructed"])
            self.assertEqual(result["guard_interventions_observed"], 0)
            self.assertFalse(result["source_continuity_certified"])


if __name__ == "__main__":
    unittest.main()
