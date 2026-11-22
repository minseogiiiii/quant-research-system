"""v13 controlled rollover and cap-contract tests. No real exchange credentials."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from audit_soak import SoakAuditError, audit
from independent_soak_replay import replay
from test_v10_soak import create_archive


def sample_policy(m, target=8000000, raw=1000000, archive=1200000):
    m["producer_release"] = "v13_rolling_bounded"
    m["max_capture_bytes"] = raw
    m["rolling_policy"] = {
        "chunk_target_bytes": target,
        "raw_budget_bytes": raw,
        "archived_jsonl_budget_bytes": archive,
        "single_message_can_exceed_chunk_target": True,
        "source_connection_continuous_across_chunk_files": True,
    }
    m["resource_telemetry"] = {
        "archive_budget_bytes": archive,
        "disk_reserve_bytes": 0,
        "archived_jsonl_bytes": sum(c["archived_line_bytes"] for c in m["chunks"]),
        "peak_rss_bytes": 100000,
        "max_write_latency_ns": 5,
        "total_write_latency_ns": 15,
        "max_fsync_latency_ns": 200,
        "min_available_disk_bytes": 1000000,
        "disk_checks": m["messages"],
    }
    return m


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "sample"
        manifest = sample_policy(create_archive(self.root))
        (self.root / "manifest.json").write_text(json.dumps(manifest))

    def write(self):
        self.root.joinpath("manifest.json").write_text(json.dumps(self.m()))

    def m(self):
        return json.loads((self.root / "manifest.json").read_text())

    def mutate(self, fn):
        m = self.m()
        fn(m)
        (self.root / "manifest.json").write_text(json.dumps(m))

    def test_v13_manifest_baseline_remains_replayable(self):
        self.assertEqual(audit(self.root)["connection_sequences"], {"0": [0, 6]})
        self.assertEqual(replay(self.root)["messages"], 7)

    def test_raw_cap_mismatch_denied(self):
        self.mutate(
            lambda m: m.__setitem__("max_capture_bytes", m["max_capture_bytes"] + 1)
        )
        with self.assertRaises(SoakAuditError):
            audit(self.root)

    def test_budget_claim_cannot_exceed_configured_cap(self):
        self.mutate(
            lambda m: m["resource_telemetry"].__setitem__(
                "archive_budget_bytes",
                m["resource_telemetry"]["archive_budget_bytes"] + 1,
            )
        )
        with self.assertRaises(SoakAuditError):
            audit(self.root)

    def test_falsified_small_chunk_target_denied(self):
        self.mutate(
            lambda m: m["rolling_policy"].__setitem__("chunk_target_bytes", 1024)
        )
        with self.assertRaises(SoakAuditError):
            audit(self.root)

    def test_unknown_research_admission_policy_denied(self):
        self.mutate(
            lambda m: m["rolling_policy"].__setitem__("historical_asof_proven", True)
        )
        with self.assertRaises(SoakAuditError):
            audit(self.root)

    def test_resource_telemetry_required(self):
        self.mutate(lambda m: m.pop("resource_telemetry"))
        with self.assertRaises(SoakAuditError):
            audit(self.root)

    def test_legacy_unsupported_elevated_raw_cap(self):
        self.mutate(
            lambda m: (
                m.pop("producer_release"),
                m.pop("rolling_policy"),
                m.__setitem__("max_capture_bytes", 1000000000),
            )
        )
        with self.assertRaises(SoakAuditError):
            audit(self.root)


@unittest.skipUnless(shutil.which("node"), "Node 22+ required")
class SyntheticRolloverTests(unittest.TestCase):
    def run_capture(self, config):
        with tempfile.TemporaryDirectory() as d:
            env = os.environ.copy()
            env.update(
                {
                    "SOAK_DURATION_SECONDS": "15",
                    "SOAK_DISK_RESERVE_BYTES": "0",
                    "SOAK_CHUNK_LIMIT_BYTES": "1024",
                    "SOAK_RAW_BUDGET_BYTES": "1000000",
                    "SOAK_ARCHIVE_BUDGET_BYTES": "1200000",
                }
            )
            env.update(config)
            cmd = [
                "node",
                "--import",
                str(ROOT / "tests/mock_coinbase_periodic.mjs"),
                str(ROOT / "scripts/btc_eth_rolling_v13.mjs"),
            ]
            result = subprocess.run(
                cmd, cwd=d, env=env, capture_output=True, text=True, timeout=23
            )
            archives = list((Path(d) / "soak_evidence").glob("coinbase_btc_eth_soak_*"))
            self.assertEqual(
                len(archives), 1, result.stdout[-300:] + result.stderr[-400:]
            )
            m = json.loads((archives[0] / "manifest.json").read_text())
            primary = audit(archives[0])
            independent = (
                replay(archives[0])
                if primary["status"] == "INTERNALLY_REPLAYED_NO_KNOWN_GAP"
                else None
            )
        return result.returncode, m, primary, independent

    def test_one_connection_many_small_chunks(self):
        code, m, primary, ind = self.run_capture({})
        self.assertEqual(code, 0)
        self.assertEqual(len(m["connections"]), 1)
        self.assertGreaterEqual(len(m["chunks"]), 3)
        self.assertEqual(primary["message_count"], ind["messages"])
        self.assertEqual(primary["connection_sequences"]["0"], [0, m["messages"] - 1])
        self.assertEqual(
            primary["books_final"]["BTC-USD"]["sha256"],
            ind["books_final"]["BTC-USD"]["sha256"],
        )
        self.assertTrue(m["ready_for_research"])
        self.assertFalse(m["orders_enabled"])

    def test_low_jsonl_budget_fails_closed(self):
        code, m, primary, ind = self.run_capture(
            {"SOAK_ARCHIVE_BUDGET_BYTES": "1200", "SOAK_RAW_BUDGET_BYTES": "1100"}
        )
        self.assertNotEqual(code, 0)
        self.assertFalse(m["ready_for_research"])
        self.assertEqual(primary["status"], "ARCHIVED_BUT_NOT_ADMITTED")


if __name__ == "__main__":
    unittest.main()
