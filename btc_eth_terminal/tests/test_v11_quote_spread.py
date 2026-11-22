"""No-fill quote cost diagnostics: synthetic and source-archived tests."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))
from quote_spread_audit import inspect
from test_v10_soak import create_archive


class QuoteSpreadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "fixture"
        create_archive(self.folder)

    def test_equal_observation_is_not_exposed_as_future_quote(self):
        # All 7 fixture packets have identical receive timestamps. At grid t0
        # no book had been observed BEFORE t0, hence not a valid quote.
        out = inspect(self.folder, step_seconds=1, max_quote_age_seconds=10)
        self.assertEqual(out["products"]["BTC-USD"]["fresh_quote_samples"], 0)
        self.assertEqual(out["products"]["ETH-USD"]["fresh_quote_samples"], 0)
        self.assertEqual(
            out["products"]["BTC-USD"]["unready_or_empty_book_samples_excluded"], 1
        )
        self.assertIsNone(out["products"]["BTC-USD"]["spread_bps_median"])
        self.assertIsNone(out["user_supplied_hypothetical_one_way_taker_fee_bp"])
        self.assertFalse(out["actual_order_fills_verified"])

    def test_fee_must_be_explicit_and_finite(self):
        for bad in ("NaN", "Infinity", "-2", "101"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                inspect(
                    self.folder,
                    step_seconds=1,
                    max_quote_age_seconds=10,
                    taker_fee_bps=bad,
                )

    def test_explicit_fee_never_claims_an_actual_fee_tier(self):
        out = inspect(self.folder, step_seconds=1, taker_fee_bps="5")
        self.assertEqual(out["user_supplied_hypothetical_one_way_taker_fee_bp"], "5")
        self.assertFalse(out["historical_fee_tier_verified"])
        self.assertFalse(out["economic_alpha_verified"])

    def test_gapped_manifest_cannot_feed_quote_stats(self):
        p = self.folder / "manifest.json"
        mf = json.loads(p.read_text())
        mf["status"] = "CAPTURE_COMPLETED_WITH_QUARANTINED_GAPS"
        mf["faults"] = [{"connection_id": 0, "reason": "GAP"}]
        mf["ready_for_research"] = False
        p.write_text(json.dumps(mf))
        with self.assertRaisesRegex(ValueError, "Gapped/invalid"):
            inspect(self.folder)

    def test_source_changes_after_admission_audit_fail_closed(self):
        # Deliberately modify on-disk bytes after the first valid source audit.
        # The spread tool must re-check bytes during its own later read.
        from unittest.mock import patch

        from audit_soak import audit as original_audit

        def alter_after_admission(folder):
            good = original_audit(folder)
            source = Path(folder) / "chunks/chunk_00000_conn_000.jsonl"
            source.write_bytes(source.read_bytes().replace(b"BTC-USD", b"ETH-USD", 1))
            return good

        with patch("quote_spread_audit.audit", side_effect=alter_after_admission):
            with self.assertRaisesRegex(ValueError, "changed after"):
                inspect(self.folder, step_seconds=1)

    def test_valid_source_statistics(self):
        out = inspect(self.folder, step_seconds=1)
        self.assertEqual(out["source_chunks"], 2)
        self.assertEqual(out["source_messages"], 7)
        self.assertTrue(out["no_orders_sent"])


if __name__ == "__main__":
    unittest.main()
