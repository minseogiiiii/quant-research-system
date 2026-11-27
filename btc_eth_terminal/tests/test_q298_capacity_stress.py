"""Boundary tests for offline capacity and long logical-time C2 acceptance."""
import tempfile
import time
import unittest
from pathlib import Path

from q296_c2_policy import RecoveryMonitor
from q298_capacity_stress import Window, stress_index
from test_q296_c2_policy import row


class CapacityContracts(unittest.TestCase):
    def test_window_boundary_and_duplicate_timestamp(self):
        window = Window(100)
        for t, size in ((0, 4), (0, 6), (99, 9), (100, 5), (199, 3)):
            window.add(t, size)
        self.assertEqual(window.report(), {
            "window_ns": 100, "max_bytes": 19, "max_messages": 3})
        with self.assertRaisesRegex(ValueError, "WINDOW_ORDER"):
            window.add(198, 1)

    def test_synthetic_trade_index_exact_and_conflict(self):
        with tempfile.TemporaryDirectory() as folder:
            result = stress_index(Path(folder), 17, time.monotonic())
        self.assertEqual(result["synthetic_rows"], 18)
        self.assertEqual(result["matching_pairs"], 9)
        self.assertTrue(result["conflict_rejected"])

    def test_five_hour_logical_clock_with_two_distinct_faults(self):
        monitor = RecoveryMonitor(18000)
        for i in range(72001):
            t = i / 4
            if t < 6300:
                body = row()
            elif t < 6305:
                body = row(both=False)
            elif t < 8129:
                body = row()
            elif t < 8130:
                body = row(ready=False, both=False, generation=2)
            else:
                body = row(generation=3, source="secondary", session="b" * 32)
            monitor.observe(t, body)
        result = monitor.finish()
        self.assertEqual(result["duration_seconds"], 18000)
        self.assertEqual(len(result["recovered_incidents"]), 2)
        self.assertEqual(result["cursor_transitions"], 1)
        self.assertFalse(result["zero_gap_certified"])

    def test_late_unrecovered_gap_never_certifies(self):
        monitor = RecoveryMonitor(18000)
        for i in range(72000):
            monitor.observe(i / 4, row())
        monitor.observe(18000, row(both=False))
        with self.assertRaisesRegex(ValueError, "FINAL_AVAILABILITY"):
            monitor.finish()


if __name__ == "__main__":
    unittest.main()
