"""Pure bounded capacity projections, never inferred profitability."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from rolling_capacity import project
from test_v10_soak import create_archive


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Path(self.tmp.name) / "raw"
        create_archive(self.p)

    def test_extrapolation_explicitly_unverified(self):
        v = project(self.p, target_seconds=3600, burst_multiplier=Decimal("3"))
        self.assertEqual(v["status"], "UNVERIFIED_CAPACITY_EXTRAPOLATION")
        self.assertFalse(v["source_replay_independently_verified_by_this_tool"])
        self.assertFalse(v["clock_offset_or_live_source_uptime_verified"])
        self.assertEqual(v["stress_multiplier_assumption"], "3")
        self.assertGreaterEqual(
            v["stress_projected_jsonl_bytes"],
            3 * v["projected_jsonl_bytes_at_observed_rate"] - 3,
        )

    def test_duplicate_manifest_key_denied(self):
        text = (self.p / "manifest.json").read_text()
        (self.p / "manifest.json").write_text(text[:-1] + ',"bytes":2}')
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            project(self.p)

    def test_utc_required(self):
        m = json.loads((self.p / "manifest.json").read_text())
        m["sampled_from_utc"] = "2026-01-01T00:00:00"
        (self.p / "manifest.json").write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError, "clock"):
            project(self.p)

    def test_bad_assumptions_denied(self):
        for multiplier in (Decimal("0"), Decimal("11"), Decimal("NaN")):
            with self.assertRaises(ValueError):
                project(self.p, burst_multiplier=multiplier)


if __name__ == "__main__":
    unittest.main()
