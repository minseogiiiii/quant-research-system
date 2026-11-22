"""Real Node collector filesystem fault injection using SYNTHETIC public WS frames.

Uses temporary workdirs, not private Coinbase market raw source, no order APIs.
Disk budget, disk reserve and SIGKILL must not promote incomplete output.
"""

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CollectorCrashStorageSafety(unittest.TestCase):
    def start(self, folder, **overrides):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable")
        work = Path(folder)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "SOAK_DURATION_SECONDS": "15",
            "SOAK_RAW_BUDGET_BYTES": "1000000000",
            "SOAK_ARCHIVE_BUDGET_BYTES": "2000000000",
            "SOAK_DISK_RESERVE_BYTES": "0",
            "SOAK_CHUNK_LIMIT_BYTES": "8000000",
            "SOAK_TEST_FIXTURE": "1",
            "SOAK_TEST_DISCONNECT_AFTER_MESSAGES": "0",
        }
        env.update(overrides)
        return subprocess.Popen(
            [
                node,
                "--import",
                str(ROOT / "tests/v36_mock_coinbase_websocket.mjs"),
                str(ROOT / "scripts/btc_eth_multiday_v20.mjs"),
            ],
            cwd=work,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def root(self, work):
        archives = list((Path(work) / "soak_evidence").glob("coinbase_btc_eth_soak_*"))
        self.assertEqual(len(archives), 1)
        return archives[0]

    def test_real_disk_budget_exhaustion_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="v36_disk_cap_") as t:
            p = self.start(
                t, SOAK_RAW_BUDGET_BYTES="1600", SOAK_ARCHIVE_BUDGET_BYTES="2000"
            )
            try:
                o, e = p.communicate(timeout=8)
            finally:
                if p.poll() is None:
                    p.kill()
                    p.communicate()
            self.assertNotEqual(p.returncode, 0)
            m = json.loads((self.root(t) / "manifest.json").read_bytes())
            self.assertNotEqual(
                m["status"], "CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH"
            )
            self.assertFalse(m["ready_for_research"])
            self.assertGreaterEqual(len(m["faults"]), 1)
            self.assertEqual(m["finish_reason"], "NONRECOVERABLE_STORAGE_LIMIT")
            self.assertTrue(
                any(
                    "SIZE_CAP" in x["reason"]
                    or "ARCHIVE_BUDGET_EXCEEDED" in x["reason"]
                    for x in m["faults"]
                )
            )

    def test_disk_reserve_exhaustion_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="v36_disk_reserve_") as t:
            p = self.start(t, SOAK_DISK_RESERVE_BYTES="1000000000000")
            try:
                o, e = p.communicate(timeout=8)
            finally:
                if p.poll() is None:
                    p.kill()
                    p.communicate()
            self.assertNotEqual(p.returncode, 0)
            m = json.loads((self.root(t) / "manifest.json").read_bytes())
            self.assertFalse(m["ready_for_research"])
            self.assertEqual(m["finish_reason"], "NONRECOVERABLE_STORAGE_LIMIT")
            self.assertTrue(
                any("DISK_RESERVE_EXCEEDED" in x["reason"] for x in m["faults"])
            )

    def test_sigkill_partial_file_and_restart_separate_run(self):
        with tempfile.TemporaryDirectory(prefix="v36_sigkill_") as t:
            a = Path(t) / "run_a"
            a.mkdir()
            p = self.start(a)
            try:
                limit = time.monotonic() + 5
                while time.monotonic() < limit:
                    items = list(
                        (a / "soak_evidence").glob(
                            "coinbase_btc_eth_soak_*/chunks/*.partial"
                        )
                    )
                    if items and items[0].stat().st_size > 500:
                        break
                    time.sleep(0.02)
                else:
                    self.fail("Collector never wrote a live partial before SIGKILL")
                os.kill(p.pid, signal.SIGKILL)
                p.communicate(timeout=3)
            finally:
                if p.poll() is None:
                    p.kill()
                    p.communicate()
            original = self.root(a)
            self.assertFalse(
                json.loads((original / "manifest.json").read_bytes())[
                    "ready_for_research"
                ]
            )
            self.assertEqual(
                json.loads((original / "manifest.json").read_bytes())["status"],
                "CAPTURING_UNVERIFIED",
            )
            self.assertTrue(list((original / "chunks").glob("*.partial")))
            # Restart never appends to the old source session's partial output.
            b = Path(t) / "run_b"
            b.mkdir()
            q = self.start(b)
            try:
                time.sleep(0.55)
                q.send_signal(signal.SIGTERM)
                q.communicate(timeout=6)
            finally:
                if q.poll() is None:
                    q.kill()
                    q.communicate()
            new = self.root(b)
            self.assertNotEqual(original.resolve(), new.resolve())
            self.assertFalse(
                json.loads((new / "manifest.json").read_bytes())["ready_for_research"]
            )
            self.assertTrue(list((original / "chunks").glob("*.partial")))


if __name__ == "__main__":
    unittest.main()
