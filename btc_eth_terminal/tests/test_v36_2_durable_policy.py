"""Unit contracts only. These do not certify an actual persistent Linux host."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from v36_observe_existing_collector import durable_policy_valid


class StrictDurableCapturePolicy(unittest.TestCase):
    def test_24_hour_production_size_policy(self):
        self.assertTrue(
            durable_policy_valid(86400, 40_000_000_000, 55_000_000_000, 10 * 1024**3)
        )

    def test_reject_shorter_than_one_hour(self):
        self.assertFalse(
            durable_policy_valid(3599, 40_000_000_000, 55_000_000_000, 10 * 1024**3)
        )

    def test_reject_over_day(self):
        self.assertFalse(
            durable_policy_valid(86401, 40_000_000_000, 55_000_000_000, 10 * 1024**3)
        )

    def test_reject_small_raw_budget(self):
        self.assertFalse(
            durable_policy_valid(86400, 20_000_000_000, 55_000_000_000, 10 * 1024**3)
        )

    def test_reject_small_archive_budget(self):
        self.assertFalse(
            durable_policy_valid(86400, 40_000_000_000, 50_000_000_000, 10 * 1024**3)
        )

    def test_reject_small_disk_reserve(self):
        self.assertFalse(
            durable_policy_valid(86400, 40_000_000_000, 55_000_000_000, 4 * 1024**3)
        )

    def test_reject_bool_budgets(self):
        self.assertFalse(
            durable_policy_valid(86400, True, 55_000_000_000, 10 * 1024**3)
        )

    def test_reject_archive_below_raw(self):
        self.assertFalse(
            durable_policy_valid(3600, 60_000_000_000, 55_000_000_000, 10 * 1024**3)
        )


if __name__ == "__main__":
    unittest.main()
