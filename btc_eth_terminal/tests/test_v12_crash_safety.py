"""Fail-closed disk-budget, manifest durability and crash-forensic tests."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))
def _q14_load_after_path_setup_0():
    from audit_soak import SoakAuditError, audit
    from inspect_crashed_soak import inspect
    from test_v10_soak import create_archive
    return SoakAuditError, audit, inspect, create_archive

SoakAuditError, audit, inspect, create_archive = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0


class CrashInspectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = Path(self.tmp.name) / "sample"
        create_archive(self.archive)

    def test_complete_archive_is_still_never_admitted_by_forensic_tool(self):
        result = inspect(self.archive)
        self.assertEqual(result["status"], "CRASH_FORENSICS_ONLY_NOT_ADMITTED")
        self.assertEqual(result["committed_chunks"], 2)
        self.assertEqual(result["complete_source_rows_seen"], 7)
        self.assertFalse(result["eligible_for_point_in_time_feature_research"])

    def test_orphan_partial_is_inventoried_not_promoted(self):
        p = self.archive / "chunks" / "chunk_00002_conn_000.jsonl.partial"
        p.write_bytes(
            (self.archive / "chunks" / "chunk_00001_conn_000.jsonl").read_bytes()
        )
        result = inspect(self.archive)
        self.assertEqual(len(result["partial_chunks"]), 1)
        self.assertFalse(result["eligible_for_point_in_time_feature_research"])
        with self.assertRaises(SoakAuditError):
            audit(self.archive)

    def test_truncated_partial_is_visible(self):
        p = self.archive / "chunks" / "chunk_00002_conn_000.jsonl.partial"
        p.write_bytes(
            (self.archive / "chunks" / "chunk_00001_conn_000.jsonl").read_bytes()[:90]
        )
        result = inspect(self.archive)
        self.assertTrue(result["any_partial_tail_detected"])
        self.assertFalse(result["eligible_for_point_in_time_feature_research"])

    def test_committed_chunk_mutation_is_rejected(self):
        p = self.archive / "chunks" / "chunk_00000_conn_000.jsonl"
        p.write_bytes(p.read_bytes().replace(b"BTC-USD", b"ETH-USD", 1))
        with self.assertRaises(SoakAuditError):
            inspect(self.archive)

    def test_missing_committed_chunk_is_rejected(self):
        (self.archive / "chunks" / "chunk_00000_conn_000.jsonl").unlink()
        with self.assertRaises(SoakAuditError):
            inspect(self.archive)

    def test_valid_resource_telemetry_crosschecks_archived_bytes(self):
        path = self.archive / "manifest.json"
        m = json.loads(path.read_text())
        n = sum(c["archived_line_bytes"] for c in m["chunks"])
        m["resource_telemetry"] = {
            "archive_budget_bytes": 8192,
            "disk_reserve_bytes": 0,
            "archived_jsonl_bytes": n,
            "peak_rss_bytes": 90000,
            "max_write_latency_ns": 10,
            "total_write_latency_ns": 100,
            "max_fsync_latency_ns": 200,
            "min_available_disk_bytes": 10000,
            "disk_checks": m["messages"],
        }
        path.write_text(json.dumps(m))
        self.assertEqual(
            audit(self.archive)["status"], "INTERNALLY_REPLAYED_NO_KNOWN_GAP"
        )
        m["resource_telemetry"]["archived_jsonl_bytes"] += 1
        path.write_text(json.dumps(m))
        with self.assertRaisesRegex(SoakAuditError, "resource telemetry"):
            audit(self.archive)

    def test_manifest_tmp_not_silently_accepted(self):
        (self.archive / "manifest.json.tmp").write_text("pending")
        result = inspect(self.archive)
        self.assertTrue(result["manifest_tmp_exists"])
        self.assertFalse(result["eligible_for_point_in_time_feature_research"])
        with self.assertRaises(SoakAuditError):
            audit(self.archive)


@unittest.skipUnless(shutil.which("node"), "Node required")
class LiveCollectorFaultTests(unittest.TestCase):
    def run_collector(self, extra=None, interrupt=False):
        with tempfile.TemporaryDirectory() as d:
            env = os.environ.copy()
            env.update({"SOAK_DURATION_SECONDS": "15", "SOAK_DISK_RESERVE_BYTES": "0"})
            env.update(extra or {})
            cmd = [
                "node",
                "--import",
                str(ROOT / "tests" / "mock_coinbase_periodic.mjs"),
                str(ROOT / "scripts" / "btc_eth_live_chunked_probe.mjs"),
            ]
            if interrupt:
                proc = subprocess.Popen(
                    cmd,
                    cwd=d,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                time.sleep(2.0)
                proc.send_signal(signal.SIGTERM)
                stdout, stderr = proc.communicate(timeout=12)
                code = proc.returncode
            else:
                proc = subprocess.run(
                    cmd, cwd=d, env=env, capture_output=True, text=True, timeout=23
                )
                code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
            roots = list((Path(d) / "soak_evidence").glob("coinbase_btc_eth_soak_*"))
            self.assertEqual(len(roots), 1, stderr)
            return (
                code,
                json.loads((roots[0] / "manifest.json").read_text()),
                inspect(roots[0]),
                stdout + stderr,
            )

    def test_positive_capture_records_resource_budget(self):
        code, m, forensic, logs = self.run_collector()
        self.assertEqual(code, 0, logs)
        t = m["resource_telemetry"]
        self.assertGreater(t["archived_jsonl_bytes"], 0)
        self.assertLessEqual(t["archived_jsonl_bytes"], t["archive_budget_bytes"])
        self.assertGreater(t["disk_checks"], 0)
        self.assertGreater(t["peak_rss_bytes"], 0)
        self.assertEqual(forensic["committed_chunks"], len(m["chunks"]))

    def test_small_archive_budget_fails_closed(self):
        code, m, forensic, logs = self.run_collector(
            {"SOAK_ARCHIVE_BUDGET_BYTES": "1024"}
        )
        self.assertNotEqual(code, 0)
        self.assertFalse(m["ready_for_research"])
        self.assertNotEqual(
            m["status"], "CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH"
        )
        self.assertGreater(len(m["faults"]), 0)
        self.assertEqual(forensic["status"], "CRASH_FORENSICS_ONLY_NOT_ADMITTED")

    def test_disk_reserve_failure_is_terminal(self):
        code, m, forensic, _ = self.run_collector(
            {"SOAK_DISK_RESERVE_BYTES": str(10**12)}
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(m["finish_reason"], "NONRECOVERABLE_STORAGE_LIMIT")
        self.assertFalse(m["ready_for_research"])
        self.assertEqual(forensic["status"], "CRASH_FORENSICS_ONLY_NOT_ADMITTED")

    def test_sigkill_leaves_forensic_partial_not_admitted(self):
        with tempfile.TemporaryDirectory() as d:
            env = os.environ.copy()
            env["SOAK_DURATION_SECONDS"] = "15"
            cmd = [
                "node",
                "--import",
                str(ROOT / "tests" / "mock_coinbase_periodic.mjs"),
                str(ROOT / "scripts" / "btc_eth_live_chunked_probe.mjs"),
            ]
            proc = subprocess.Popen(
                cmd,
                cwd=d,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(1.0)
            proc.kill()
            proc.communicate(timeout=5)
            roots = list((Path(d) / "soak_evidence").glob("coinbase_btc_eth_soak_*"))
            self.assertEqual(len(roots), 1)
            root = roots[0]
            result = inspect(root)
            self.assertFalse(result["eligible_for_point_in_time_feature_research"])
            self.assertEqual(result["manifest_status"], "CAPTURING_UNVERIFIED")
            self.assertGreaterEqual(len(result["partial_chunks"]), 1)
            with self.assertRaises(SoakAuditError):
                audit(root)

    def test_graceful_sigterm_preserves_evidence_but_quarantines(self):
        code, m, forensic, logs = self.run_collector(interrupt=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(m["finish_reason"], "SIGTERM_QUARANTINE")
        self.assertFalse(m["ready_for_research"])
        self.assertEqual(forensic["status"], "CRASH_FORENSICS_ONLY_NOT_ADMITTED")


if __name__ == "__main__":
    unittest.main()
