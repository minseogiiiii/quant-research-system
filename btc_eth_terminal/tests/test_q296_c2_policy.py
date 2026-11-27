"""Deterministic time, epoch, resource and recovery acceptance boundaries."""
from __future__ import annotations

import unittest

from q293_long_policy import admission
from q296_c2_policy import RecoveryMonitor


def row(*, ready=True, both=True, generation=1, source="primary",
        session="a" * 32, attempts=None, run="f" * 32):
    return {
        "gateway_run": run, "generation": generation,
        "source_session": session if ready else None,
        "active_source": source if ready else None,
        "status": "HEALTHY" if ready else "QUARANTINED",
        "states": dict.fromkeys(("BTC-USD", "ETH-USD"), {}) if ready else None,
        "checkpoint_comparison": dict.fromkeys(
            ("BTC-USD", "ETH-USD"),
            "NOT_COMPARABLE" if both else "SOURCE_UNAVAILABLE",
        ),
        "source_attempts": attempts or {"primary": 1, "secondary": 1},
        "real_orders_enabled": False, "zero_gap_certified": False,
        "historical_gap_recovered": False,
    }


class C2Acceptance(unittest.TestCase):
    def test_full_logical_1800s_with_standby_gap_and_active_switch(self):
        m = RecoveryMonitor(1800)
        for n in range(7201):
            now = n / 4
            if now < 10:
                body = row()
            elif now < 14:
                body = row(both=False)
            elif now < 20:
                body = row()
            elif now < 21:
                body = row(ready=False, both=False, generation=2)
            else:
                body = row(generation=3, source="secondary", session="b" * 32)
            m.observe(now, body)
        result = m.finish()
        self.assertEqual(len(result["recovered_incidents"]), 2)
        self.assertEqual(result["cursor_transitions"], 1)
        self.assertGreaterEqual(result["outage_upper_bound_seconds"], 1)
        self.assertGreaterEqual(result["degraded_upper_bound_seconds"], 5)
        self.assertFalse(result["zero_gap_certified"])

    def test_old_cursor_cannot_resurface_after_quarantine(self):
        m = RecoveryMonitor(1800)
        m.observe(0, row())
        m.observe(1, row(ready=False, both=False, generation=1))
        with self.assertRaisesRegex(ValueError, "RETIRED_CURSOR"):
            m.observe(2, row())

    def test_generation_cannot_regress(self):
        m = RecoveryMonitor(1800)
        m.observe(0, row(generation=2))
        with self.assertRaisesRegex(ValueError, "C2_GENERATION"):
            m.observe(1, row(generation=1))

    def test_same_session_cannot_be_republished_as_new_generation(self):
        m = RecoveryMonitor(1800)
        m.observe(0, row())
        with self.assertRaisesRegex(ValueError, "C2_CURSOR_REUSE"):
            m.observe(1, row(generation=2))

    def test_both_source_loss_times_out(self):
        m = RecoveryMonitor(1800)
        m.observe(0, row())
        for n in range(1, 6):
            m.observe(n, row(ready=False, both=False, generation=2))
        with self.assertRaisesRegex(ValueError, "C2_OUTAGE_TIMEOUT"):
            m.observe(6, row(ready=False, both=False, generation=2))

    def test_standby_recovery_timeout(self):
        m = RecoveryMonitor(1800)
        m.observe(0, row())
        for n in range(1, 21):
            m.observe(n, row(both=False))
        with self.assertRaisesRegex(ValueError, "C2_RECOVERY_TIMEOUT"):
            m.observe(21, row(both=False))

    def test_poll_gap_or_bad_clock_is_failure(self):
        m = RecoveryMonitor(1800)
        m.observe(0, row())
        with self.assertRaisesRegex(ValueError, "C2_POLL_GAP"):
            m.observe(6, row())
        with self.assertRaisesRegex(ValueError, "C2_CLOCK"):
            m.observe(float("nan"), row())

    def test_restart_exhaustion_is_failure(self):
        m = RecoveryMonitor(1800)
        m.observe(0, row())
        with self.assertRaisesRegex(ValueError, "C2_RESTART_BUDGET"):
            m.observe(1, row(attempts={"primary": 3, "secondary": 1}))

    def test_unsafe_permission_and_divergence(self):
        m = RecoveryMonitor(1800)
        with self.assertRaisesRegex(ValueError, "C2_AUTHORITY"):
            m.observe(0, {**row(), "real_orders_enabled": True})
        bad = row()
        bad["checkpoint_comparison"]["BTC-USD"] = "DIVERGENCE"
        with self.assertRaisesRegex(ValueError, "C2_CROSS_SOURCE_DIVERGENCE"):
            RecoveryMonitor(1800).observe(0, bad)

    def test_incomplete_duration_never_passes(self):
        m = RecoveryMonitor(1800)
        m.observe(0, row())
        m.observe(1, row())
        with self.assertRaisesRegex(ValueError, "C2_FINAL_AVAILABILITY"):
            m.finish()

    def test_five_hour_storage_bound_includes_four_attempts(self):
        self.assertGreater(admission(18000, 1), 64_800_000_000)
        self.assertGreater(admission(18000, 1), admission(18000, 0))


if __name__ == "__main__":
    unittest.main()
