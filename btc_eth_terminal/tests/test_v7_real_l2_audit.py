"""Offline source-integrity tests for the short 2026-11-13 Coinbase public L2 pilot.

Not an independent full order-book reconciliation or a profitable strategy test.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def _q14_load_after_path_setup_0():
    from audit_l2_pilot import L2AuditError, _decimal, _json, _utc, audit_session

    return L2AuditError, _decimal, _json, _utc, audit_session


L2AuditError, _decimal, _json, _utc, audit_session = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0


class L2SourceAuditTests(unittest.TestCase):
    def test_real_public_l2_archived_message_integrity(self):
        folder = ROOT / "backend" / "data" / "l2_pilot_20261113_205058UTC"
        report = audit_session(folder)
        self.assertEqual(report["received_message_count"], 885)
        self.assertEqual(report["observed_connection_sequence_nonunit_increments"], 0)
        self.assertEqual(
            report["per_product"]["BTC-USD"]["individual_trades_reported"], 322
        )
        self.assertEqual(
            report["per_product"]["ETH-USD"]["individual_trades_reported"], 180
        )
        self.assertFalse(report["alpha_verified"])
        self.assertFalse(report["independent_source_reference_book_verified"])

    def test_reject_json_duplicate_properties(self):
        with self.assertRaises(L2AuditError):
            _json('{"p":1,"p":2}')

    def test_reject_invalid_book_quantities(self):
        for bad in ("NaN", "Infinity", "-0.001", ""):
            with self.subTest(value=bad), self.assertRaises(L2AuditError):
                _decimal(bad, zero_ok=True)

    def test_reject_naive_capture_timestamp(self):
        with self.assertRaises(L2AuditError):
            _utc("2026-11-13T20:51:00")


if __name__ == "__main__":
    unittest.main()
