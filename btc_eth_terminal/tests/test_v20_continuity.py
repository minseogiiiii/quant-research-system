"""Model-level source boundary tests; synthetic source headers never market evidence."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from cohort_continuity_v20 import ContinuityError, validate_intervals


def row(k, start, end, seconds=3600, **extra):
    x = {
        "path": str(k),
        "manifest_sha256": str(k) * 64,
        "sampled_from_utc": start,
        "finished_at_utc": end,
        "required_duration_seconds": seconds,
        "single_connection": True,
        "fault_count": 0,
        "primary_accepted": True,
        "secondary_replay_accepted": True,
        "message_count": 100,
        "chunks": 2,
        "producer_release": "v20_multiday_bounded",
    }
    x.update(extra)
    return x


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.one = row("a", "2026-11-14T00:00:00Z", "2026-11-14T01:00:00Z")
        self.two = row("b", "2026-11-14T01:00:00Z", "2026-11-14T02:00:00Z")

    def test_even_adjacent_files_have_unproven_source_continuity(self):
        x = validate_intervals([self.one, self.two])
        self.assertFalse(x["single_connection_24h_internal_evidence"])
        self.assertTrue(x["boundaries"][0]["has_unreconciled_connection_boundary"])

    def test_gap_is_explicit(self):
        self.two["sampled_from_utc"] = "2026-11-14T01:00:01Z"
        x = validate_intervals([self.one, self.two])
        self.assertEqual(x["boundaries"][0]["wall_clock_gap_seconds"], 1)

    def test_overlap_is_still_unproven(self):
        self.two["sampled_from_utc"] = "2026-11-14T00:59:59Z"
        x = validate_intervals([self.one, self.two])
        self.assertEqual(x["boundaries"][0]["wall_clock_gap_seconds"], -1)
        self.assertFalse(
            x["boundaries"][0]["continuous_trading_or_training_join_allowed"]
        )

    def test_rejected_dual_replay(self):
        with self.assertRaisesRegex(ContinuityError, "dual-replay"):
            validate_intervals([dict(self.one, secondary_replay_accepted=False)])

    def test_faulted_source_rejected(self):
        with self.assertRaisesRegex(ContinuityError, "Non-admissible"):
            validate_intervals([dict(self.one, fault_count=1)])

    def test_two_connections_rejected(self):
        with self.assertRaisesRegex(ContinuityError, "Non-admissible"):
            validate_intervals([dict(self.one, single_connection=False)])

    def test_same_source_duplicate_rejected(self):
        with self.assertRaisesRegex(ContinuityError, "Duplicate"):
            validate_intervals([self.one, self.one])

    def test_one_verified_24h_can_have_narrow_internal_status(self):
        r = row("c", "2026-11-14T00:00:00Z", "2026-11-15T00:00:00Z", 86400)
        x = validate_intervals([r])
        self.assertTrue(x["single_connection_24h_internal_evidence"])
        self.assertFalse(x["independently_time_aligned_exchange_truth_proven"])

    def test_forged_short_duration_not_24h(self):
        r = row("c", "2026-11-14T00:00:00Z", "2026-11-15T00:00:00Z", 3600)
        self.assertFalse(
            validate_intervals([r])["single_connection_24h_internal_evidence"]
        )

    def test_invalid_utc_rejected(self):
        with self.assertRaisesRegex(ContinuityError, "UTC"):
            validate_intervals(
                [dict(self.one, sampled_from_utc="2026-11-14T00:00:00-05:00")]
            )


if __name__ == "__main__":
    unittest.main()
