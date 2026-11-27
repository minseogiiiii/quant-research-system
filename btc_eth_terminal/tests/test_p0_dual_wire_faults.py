"""Real isolated processes with synthetic-only source faults; never exchange I/O."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from q292_isolated_observer import ObserverProcess
from q292_resilient_gateway import ManagedSource, ResilientRoute
from q293_b4_diagnostics import manifest_receipt, observer_receipt
from q293_dual_1800s import capture_path


def until(route, predicate, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        body = route.read()
        if predicate(body):
            return body
        time.sleep(.06)
    raise AssertionError("SYNTHETIC_DUAL_SOURCE_DEADLINE")


def ready(body):
    return (
        body["active_source"] is not None
        and len(body["checkpoint_comparison"]) == 2
        and set(body["checkpoint_comparison"].values()) <= {"MATCH", "NOT_COMPARABLE"}
    )


class DualWireFaultContracts(unittest.TestCase):
    def test_fixture_fault_cannot_be_used_in_live_mode(self):
        with self.assertRaisesRegex(ValueError, "REQUIRES_SYNTHETIC"):
            ObserverProcess("secondary", Path("/unused"), 30, False,
                            fixture_fault="duplicate")

    def _fault(self, kind):
        with tempfile.TemporaryDirectory(prefix="q292_wire_") as directory:
            root = Path(directory)

            def factory(name, work, duration, synthetic):
                return ObserverProcess(name, work, duration, synthetic,
                                       fixture_fault=kind)

            p = ManagedSource("primary", root / "p", 30, True, max_restarts=0)
            s = ManagedSource("secondary", root / "s", 30, True,
                              factory=factory, max_restarts=0)
            route = ResilientRoute(p, s)
            try:
                first = until(route, ready, 8)
                # Guarantee active source remains healthy during standby recovery.
                # If initial arbitration preferred secondary, this fixture cannot
                # certify the intended standby scenario.
                self.assertEqual(first["active_source"], "primary")
                old = s.admitted_session
                self.assertIsNotNone(old)
                lost = until(route, lambda x: not ready(x), 12)
                self.assertEqual(lost["active_source"], "primary")
                self.assertEqual(lost["generation"], first["generation"])
                recovered = until(route, ready, 12)
                self.assertEqual(recovered["active_source"], "primary")
                self.assertEqual(recovered["generation"], first["generation"])
                self.assertNotEqual(old, s.admitted_session)
                self.assertIn(old, s.retired_sessions)
                self.assertEqual(s.attempts, 1)
                self.assertFalse(recovered["zero_gap_certified"])
                self.assertFalse(recovered["historical_gap_recovered"])
            finally:
                p.close()
                s.close()
            archive = capture_path(s.root / "attempt_01")
            self.assertIsNotNone(archive)
            manifest = json.loads((archive / "manifest.json").read_bytes())
            self.assertIs(manifest["synthetic_fixture"], True)
            self.assertGreaterEqual(len(manifest["connections"]), 2)
            self.assertFalse(manifest["ready_for_research"])
            receipt = manifest_receipt(archive / "manifest.json")
            self.assertEqual(receipt["status"], "MANIFEST_READ")
            if kind == "duplicate":
                self.assertEqual(receipt["first_fault_class"], "DUP_SNAPSHOT")
                self.assertEqual(len(receipt["duplicate_snapshot_receipts"]), 1)
            elif kind == "sequence":
                self.assertEqual(receipt["first_fault_class"], "SEQUENCE_GAP")
            observer = observer_receipt(s.root / "attempt_01/observer_events.jsonl")
            self.assertEqual(observer["status"], "OBSERVER_JOURNAL_READ")

    def test_duplicate_snapshot_standby(self):
        self._fault("duplicate")

    def test_sequence_gap_standby(self):
        self._fault("sequence")

    def test_heartbeat_stale_standby(self):
        self._fault("heartbeat")


if __name__ == "__main__":
    unittest.main()
