"""V12 *real* Coinbase chunked source, producer telemetry and two replay engines."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from audit_soak import SoakAuditError, audit
from independent_soak_replay import replay
from inspect_crashed_soak import inspect

SOURCE = ROOT / "backend/data/live_disk_safe_180s_20261114/coinbase_btc_eth_soak_CWFeG3"


class RealLiveV12Admission(unittest.TestCase):
    def test_observed_180s_source_and_independent_book_parity(self):
        self.assertTrue(
            SOURCE.is_dir(), "Mandatory real public WebSocket capture missing"
        )
        a = audit(SOURCE)
        b = replay(SOURCE)
        self.assertEqual(a["status"], "INTERNALLY_REPLAYED_NO_KNOWN_GAP")
        self.assertEqual(a["message_count"], b["messages"])
        self.assertEqual(a["message_count"], 7264)
        self.assertEqual(a["chunk_count"], b["chunks"])
        self.assertEqual(a["chunk_count"], 3)
        self.assertEqual(a["connection_sequences"], {"0": [0, 7263]})
        self.assertEqual(a["books_final"], b["books_final"])
        for p in ("BTC-USD", "ETH-USD"):
            for key, value in b["per_product"][p].items():
                self.assertEqual(a["per_product"][p].get(key, 0), value, (p, key))
        m = json.loads((SOURCE / "manifest.json").read_text())
        telemetry = m["resource_telemetry"]
        self.assertEqual(
            telemetry["archived_jsonl_bytes"],
            sum(x["archived_line_bytes"] for x in m["chunks"]),
        )
        self.assertEqual(telemetry["disk_checks"], a["message_count"])
        self.assertFalse(a["executable_alpha_verified"])

    def test_real_capture_forensic_inspector_never_promotes_trading(self):
        report = inspect(SOURCE)
        self.assertEqual(report["committed_chunks"], 3)
        self.assertEqual(report["complete_source_rows_seen"], 7264)
        self.assertEqual(report["partial_chunks"], [])
        self.assertFalse(report["eligible_for_point_in_time_feature_research"])

    def test_forged_disk_counters_do_not_pass_strict_admission(self):
        with tempfile.TemporaryDirectory() as d:
            replica = Path(d) / SOURCE.name
            shutil.copytree(SOURCE, replica)
            mp = replica / "manifest.json"
            m = json.loads(mp.read_text())
            m["resource_telemetry"]["archived_jsonl_bytes"] = 0
            mp.write_text(json.dumps(m))
            with self.assertRaisesRegex(SoakAuditError, "resource telemetry"):
                audit(replica)


if __name__ == "__main__":
    unittest.main()
