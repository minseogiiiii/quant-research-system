"""Controlled, synthetic-only Node durable collector tests, NEVER genuine venue captures."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def _q14_load_after_path_setup_0():
    from audit_soak import SoakAuditError, audit
    from independent_soak_replay import replay

    return SoakAuditError, audit, replay


SoakAuditError, audit, replay = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0


@unittest.skipUnless(shutil.which("node"), "Node required")
class DurableCollectorTests(unittest.TestCase):
    def command(self):
        return [
            "node",
            "--import",
            str(ROOT / "tests/mock_coinbase_periodic.mjs"),
            str(ROOT / "scripts/btc_eth_multiday_v20.mjs"),
        ]

    def test_15s_mock_with_synthetic_marker_is_never_real_source(self):
        with tempfile.TemporaryDirectory() as d:
            env = os.environ.copy()
            env.update(
                {
                    "SOAK_TEST_FIXTURE": "1",
                    "SOAK_DURATION_SECONDS": "15",
                    "SOAK_DISK_RESERVE_BYTES": "0",
                    "SOAK_CHUNK_LIMIT_BYTES": "1024",
                    "SOAK_RAW_BUDGET_BYTES": "1000000",
                    "SOAK_ARCHIVE_BUDGET_BYTES": "1200000",
                }
            )
            cp = subprocess.run(
                self.command(),
                cwd=d,
                env=env,
                capture_output=True,
                text=True,
                timeout=26,
            )
            roots = list((Path(d) / "soak_evidence").glob("coinbase_btc_eth_soak_*"))
            self.assertEqual(len(roots), 1, cp.stdout + cp.stderr)
            mf = json.loads((roots[0] / "manifest.json").read_text())
            self.assertEqual(mf["producer_release"], "v20_multiday_bounded")
            self.assertTrue(mf["synthetic_fixture"])
            self.assertFalse(mf["ready_for_research"])
            self.assertNotEqual(cp.returncode, 0)
            self.assertGreaterEqual(len(mf["chunks"]), 2)
            primary = audit(roots[0])
            secondary = replay(roots[0])
            self.assertEqual(primary["status"], "ARCHIVED_BUT_NOT_ADMITTED")
            self.assertEqual(primary["message_count"], secondary["messages"])

    def test_invalid_capture_configuration_rejected_immediately(self):
        with tempfile.TemporaryDirectory() as d:
            env = os.environ.copy()
            env.update({"SOAK_DURATION_SECONDS": "259201", "SOAK_TEST_FIXTURE": "1"})
            cp = subprocess.run(
                self.command(),
                cwd=d,
                env=env,
                capture_output=True,
                text=True,
                timeout=6,
            )
            self.assertNotEqual(cp.returncode, 0)
            self.assertIn("duration 15..259200", cp.stderr)

    def test_unexpected_process_kill_leaves_unverified_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            env = os.environ.copy()
            env.update(
                {
                    "SOAK_TEST_FIXTURE": "1",
                    "SOAK_DURATION_SECONDS": "15",
                    "SOAK_DISK_RESERVE_BYTES": "0",
                }
            )
            cp = subprocess.Popen(
                self.command(),
                cwd=d,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(1.3)
            cp.kill()
            cp.communicate(timeout=4)
            roots = list((Path(d) / "soak_evidence").glob("coinbase_btc_eth_soak_*"))
            self.assertEqual(len(roots), 1)
            m = json.loads((roots[0] / "manifest.json").read_text())
            self.assertEqual(m["status"], "CAPTURING_UNVERIFIED")
            self.assertFalse(m["ready_for_research"])
            with self.assertRaises(SoakAuditError):
                audit(roots[0])

    def test_reconnect_gets_fresh_snapshots_but_quarantines_full_source(self):
        with tempfile.TemporaryDirectory() as d:
            env = os.environ.copy()
            env.update(
                {
                    "SOAK_TEST_FIXTURE": "1",
                    "SOAK_DURATION_SECONDS": "15",
                    "SOAK_TEST_DISCONNECT_AFTER_MESSAGES": "7",
                    "SOAK_DISK_RESERVE_BYTES": "0",
                    "SOAK_CHUNK_LIMIT_BYTES": "1024",
                    "SOAK_RAW_BUDGET_BYTES": "1000000",
                    "SOAK_ARCHIVE_BUDGET_BYTES": "1200000",
                }
            )
            cp = subprocess.run(
                self.command(),
                cwd=d,
                env=env,
                capture_output=True,
                text=True,
                timeout=26,
            )
            roots = list((Path(d) / "soak_evidence").glob("coinbase_btc_eth_soak_*"))
            self.assertEqual(len(roots), 1, cp.stdout + cp.stderr)
            mf = json.loads((roots[0] / "manifest.json").read_text())
            self.assertNotEqual(cp.returncode, 0)
            self.assertFalse(mf["ready_for_research"])
            self.assertEqual(mf["total_reconnects"], 1)
            self.assertEqual(len(mf["connections"]), 2)
            self.assertTrue(mf["connections"][0]["integrity_fault"])
            self.assertFalse(mf["connections"][1]["integrity_fault"])
            self.assertTrue(
                all(all(conn["snapshots"].values()) for conn in mf["connections"])
            )
            main = audit(roots[0])
            other = replay(roots[0])
            self.assertEqual(main["status"], "ARCHIVED_BUT_NOT_ADMITTED")
            self.assertEqual(main["connection_sequences"]["1"][0], 0)
            self.assertEqual(main["books_final"], other["books_final"])


if __name__ == "__main__":
    unittest.main()
