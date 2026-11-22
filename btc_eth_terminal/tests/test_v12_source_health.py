"""Structured proof-level separation for future read-only source-health UI."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))
def _q14_load_after_path_setup_0():
    from source_health_report import report
    from test_v10_soak import create_archive
    return report, create_archive

report, create_archive = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0


class SourceHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "archive"
        create_archive(self.root)

    def test_source_report_never_claims_live_or_executable_alpha(self):
        r = report(self.root)
        self.assertEqual(r["status"], "ARCHIVED_SOURCE_INTERNALLY_REPLAYED")
        self.assertEqual(r["recorded_messages"], 7)
        self.assertEqual(r["source_sequences"], {"0": [0, 6]})
        self.assertEqual(len(r["source_chunk_sha256"]), 2)
        self.assertEqual(r["live_feed_freshness_status"], "NOT_A_LIVE_CONNECTION")
        self.assertFalse(r["available_to_trade"])
        self.assertFalse(r["executable_alpha_verified"])
        self.assertFalse(r["exchange_clock_calibrated"])

    def test_source_health_fails_closed_on_corrupt_source(self):
        p = self.root / "chunks/chunk_00001_conn_000.jsonl"
        p.write_bytes(p.read_bytes().replace(b"BTC-USD", b"ETH-USD", 1))
        r = report(self.root)
        self.assertEqual(r["status"], "SOURCE_HEALTH_BLOCKED")
        self.assertFalse(r["can_use_for_archived_data_integrity_research"])
        self.assertFalse(r["live_orders_enabled"])

    def test_unknown_source_is_not_assumed_healthy(self):
        r = report(self.root / "does-not-exist")
        self.assertEqual(r["status"], "SOURCE_HEALTH_BLOCKED")
        self.assertFalse(r["available_to_trade"])


if __name__ == "__main__":
    unittest.main()
