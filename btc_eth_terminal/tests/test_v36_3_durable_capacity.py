"""Strict local capacity preflight for 24h mode. No real capture launched."""

import sys
import tempfile
import unittest
from pathlib import Path
from shutil import _ntuple_diskusage
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _q14_load_after_path_setup_0():
    from v36_observe_existing_collector import durable_disk_capacity_sufficient

    return durable_disk_capacity_sufficient


durable_disk_capacity_sufficient = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0

BUDGET = 55_000_000_000
RESERVE = 10 * 1024**3


class DurableDiskPreflight(unittest.TestCase):
    def test_insufficient_total_capacity_fails_before_capture(self):
        with tempfile.TemporaryDirectory() as path:
            with patch(
                "v36_observe_existing_collector.shutil.disk_usage",
                return_value=_ntuple_diskusage(60_000_000_000, 0, 60_000_000_000),
            ):
                self.assertFalse(
                    durable_disk_capacity_sufficient(Path(path), BUDGET, RESERVE)
                )

    def test_sufficient_capacity_satisfies_preflight_only(self):
        with tempfile.TemporaryDirectory() as path:
            needed = BUDGET + RESERVE
            with patch(
                "v36_observe_existing_collector.shutil.disk_usage",
                return_value=_ntuple_diskusage(needed, 0, needed),
            ):
                self.assertTrue(
                    durable_disk_capacity_sufficient(Path(path), BUDGET, RESERVE)
                )

    def test_missing_or_symlink_parent_is_rejected(self):
        with tempfile.TemporaryDirectory() as path:
            base = Path(path)
            self.assertFalse(
                durable_disk_capacity_sufficient(base / "missing", BUDGET, RESERVE)
            )
            (base / "linked").symlink_to(base, target_is_directory=True)
            self.assertFalse(
                durable_disk_capacity_sufficient(base / "linked", BUDGET, RESERVE)
            )

    def test_bool_or_small_limits_rejected(self):
        with tempfile.TemporaryDirectory() as path:
            self.assertFalse(
                durable_disk_capacity_sufficient(Path(path), True, RESERVE)
            )
            self.assertFalse(durable_disk_capacity_sufficient(Path(path), BUDGET, True))
            self.assertFalse(
                durable_disk_capacity_sufficient(Path(path), BUDGET, 2 * 1024**3)
            )

    def test_filesystem_query_failure_fails_closed(self):
        with tempfile.TemporaryDirectory() as path:
            with patch(
                "v36_observe_existing_collector.shutil.disk_usage",
                side_effect=OSError("storage unavailable"),
            ):
                self.assertFalse(
                    durable_disk_capacity_sufficient(Path(path), BUDGET, RESERVE)
                )


if __name__ == "__main__":
    unittest.main()
