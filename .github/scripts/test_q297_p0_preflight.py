"""Adversarial tests against authenticated real P0 receipts, no market I/O."""
from __future__ import annotations

import copy
import json
import os
import unittest
from pathlib import Path

from q297_p0_preflight import audit


class PreflightEvidence(unittest.TestCase):
    def setUp(self):
        root = Path(os.environ["Q297_RECEIPTS"])
        self.b5 = json.loads((root / "b5/b5.json").read_bytes())
        self.c2 = json.loads((root / "c2/c2.json").read_bytes())

    def test_untampered_receipts_capacity_and_no_approval(self):
        result = audit(self.b5, self.c2)
        self.assertTrue(result["empirical_capacity_estimate_pass"])
        self.assertFalse(result["five_hour_approved"])
        self.assertFalse(result["instantaneous_peak_and_audit_rss_measured"])
        self.assertFalse(result["raw_archives_reaudited_here"])
        self.assertEqual(result["observed_market_messages"], 283679)

    def test_forged_c2_pass_without_actual_data_rejected(self):
        for field, value in (
            ("synthetic", True), ("run_id", 123), ("five_hour_resilience_certified", True),
            ("observed_seconds", 100), ("source_sha", "a" * 40),
            ("trade_index_rows", 1),
        ):
            with self.subTest(field=field):
                invalid = copy.deepcopy(self.c2)
                invalid[field] = value
                with self.assertRaises(ValueError):
                    audit(self.b5, invalid)

    def test_missing_role_or_excess_recovery_rejected(self):
        for field, value in (("role", "active"), ("source", "invalid")):
            invalid = copy.deepcopy(self.c2)
            invalid["injections"][0][field] = value
            with self.assertRaises(ValueError):
                audit(self.b5, invalid)
        self.c2["availability"]["recovered_incidents"][0]["recovery_seconds"] = 21
        with self.assertRaises(ValueError):
            audit(self.b5, self.c2)

    def test_trade_divergence_rejected(self):
        self.c2["trade_id_crosscheck"]["BTC-USD"]["divergent_ids"] = 1
        with self.assertRaisesRegex(ValueError, "TRADE_JOIN"):
            audit(self.b5, self.c2)

    def test_archive_tail_and_hash_mismatch_rejected(self):
        for field, value in (("unsealed_entries_excluded", 1),
                             ("manifest_sha256", "1" * 64)):
            invalid = copy.deepcopy(self.c2)
            invalid["archives"]["primary:attempt_01"]["audit"][field] = value
            with self.assertRaisesRegex(ValueError, "ARCHIVE_INTEGRITY"):
                audit(self.b5, invalid)

    def test_short_archive_cannot_be_extrapolated(self):
        epoch = self.c2["archives"]["primary:attempt_01"]["audit"]["epochs"][0]
        epoch["last_monotonic_ns"] = epoch["first_monotonic_ns"] + 1
        with self.assertRaisesRegex(ValueError, "ARCHIVE_SPAN"):
            audit(self.b5, self.c2)

    def test_capacity_overflow_rejected(self):
        self.c2["archives"]["primary:attempt_01"]["audit"]["sealed_bytes"] *= 100
        with self.assertRaisesRegex(ValueError, "CAPACITY"):
            audit(self.b5, self.c2)


if __name__ == "__main__":
    unittest.main()
