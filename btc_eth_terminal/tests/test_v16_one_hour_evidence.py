"""Pinned one-hour provider source and hedged order-book index correctness."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))
def _q14_load_after_path_setup_0():
    from audit_soak import SoakAuditError, audit
    from audit_v16_one_hour import OneHourEvidenceError, audit_one_hour
    from independent_soak_replay import IndependentReplayError, replay
    from test_v10_soak import create_archive
    return SoakAuditError, audit, OneHourEvidenceError, audit_one_hour, IndependentReplayError, replay, create_archive

SoakAuditError, audit, OneHourEvidenceError, audit_one_hour, IndependentReplayError, replay, create_archive = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0


class BookIndexAgreement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / "toy"

    def test_removed_reinserted_prices_cannot_leave_wrong_heap_top(self):
        def mutate(events):
            event = events[3]["events"][0]
            src = event["updates"][0]
            event["updates"] = [
                dict(src, price_level="100", new_quantity="0"),
                dict(src, price_level="99", new_quantity="3"),
                dict(src, price_level="100", new_quantity="4"),
                dict(src, price_level="99", new_quantity="0"),
            ]

        create_archive(self.source, mutate=mutate)
        primary = audit(self.source)
        secondary = replay(self.source)
        self.assertEqual(primary["books_final"], secondary["books_final"])
        self.assertEqual(primary["books_final"]["BTC-USD"]["best_bid"], "100")
        self.assertEqual(primary["books_final"]["BTC-USD"]["best_ask"], "101")

    def test_restored_price_crossed_book_is_rejected_in_both_independent_parsers(self):
        def mutate(events):
            event = events[3]["events"][0]
            src = event["updates"][0]
            event["updates"] = [
                dict(src, price_level="100", new_quantity="0"),
                dict(src, price_level="101", new_quantity="4"),
            ]

        create_archive(self.source, mutate=mutate)
        with self.assertRaisesRegex(SoakAuditError, "Crossed|locked"):
            audit(self.source)
        with self.assertRaisesRegex(IndependentReplayError, "crossed|locked"):
            replay(self.source)

    def test_one_hour_missing_or_forged_manifest_cannot_pass(self):
        source = Path(self.tmp.name) / "coinbase_btc_eth_soak_eE9SNH"
        source.mkdir()
        source.joinpath("manifest.json").write_text('{"orders_enabled":false}')
        with self.assertRaises(OneHourEvidenceError):
            audit_one_hour(source)


class VerifiedRealHour(unittest.TestCase):
    def test_pinned_3600s_source_two_replayers_and_heartbeat_census(self):
        source = ROOT / "backend/data/live_v16_one_hour/coinbase_btc_eth_soak_eE9SNH"
        result = audit_one_hour(source)
        self.assertEqual(result["source_messages"], 152069)
        self.assertEqual(result["chunks"], 41)
        self.assertEqual(result["connection_count"], 1)
        self.assertEqual(result["recorded_source_sequence"], [0, 152068])
        self.assertTrue(3600 <= result["observation_seconds"] <= 3605)
        self.assertEqual(
            result["receive_time_census"]["channel_counts"]["heartbeats"], 3599
        )
        self.assertTrue(result["verified_l2_books"]["BTC-USD"]["sha256"])
        self.assertFalse(result["actual_fills_verified"])
        self.assertFalse(result["economic_alpha_verified"])
        self.assertFalse(result["exchange_clock_synchronized"])


if __name__ == "__main__":
    unittest.main()
