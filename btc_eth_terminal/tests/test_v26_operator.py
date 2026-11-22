"""v26 operator CLI fail closed. Tests never connect to Coinbase."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "v26_operator_test", ROOT / "scripts" / "v26_capture_operator.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CaptureOperatorSafety(TestCase):
    def cli(self, work, observer, *extra):
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/v26_capture_operator.py"),
                "--workdir",
                str(work),
                "--evidence-dir",
                str(observer),
                "--raw-budget",
                "1048576",
                "--archive-budget",
                "2097152",
                "--disk-reserve",
                "0",
                *extra,
            ],
            text=True,
            capture_output=True,
            timeout=15,
        )

    def test_preflight_only_does_not_create_raw_source(self):
        with TemporaryDirectory() as t:
            root = Path(t)
            work = root / "work"
            work.mkdir()
            p = self.cli(work, root / "preflight")
            self.assertEqual(p.returncode, 2, p.stderr)
            status = json.loads((root / "preflight" / "V26_PREFLIGHT.json").read_text())
            self.assertIn("PUBLIC_COINBASE_DNS_UNAVAILABLE", status["failures"])
            self.assertFalse((work / "soak_evidence").exists())
            self.assertFalse(
                (root / "preflight" / "V25_OPERATOR_EVIDENCE.json").exists()
            )

    def test_execute_blocked_preflight_no_source(self):
        with TemporaryDirectory() as t:
            root = Path(t)
            work = root / "work"
            work.mkdir()
            p = self.cli(work, root / "preflight", "--execute-real-public-capture")
            self.assertNotEqual(p.returncode, 0)
            self.assertFalse((work / "soak_evidence").exists())

    def test_evidence_subdir_of_work_rejected(self):
        with TemporaryDirectory() as t:
            root = Path(t)
            work = root / "work"
            work.mkdir()
            p = self.cli(work, work / "danger")
            self.assertNotEqual(p.returncode, 0)
            self.assertFalse((work / "danger").exists())

    def test_existing_evidence_refused(self):
        with TemporaryDirectory() as t:
            root = Path(t)
            work = root / "work"
            work.mkdir()
            (root / "existing").mkdir()
            p = self.cli(work, root / "existing")
            self.assertNotEqual(p.returncode, 0)

    def test_crosscheck_blocked_cli_exits_nonzero(self):
        with TemporaryDirectory() as t:
            root = Path(t)
            src = root / "src"
            src.mkdir()
            ev = root / "evidence"
            ev.mkdir()
            result = root / "result.json"
            cp = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "backend/v26_capture_crosscheck.py"),
                    str(src),
                    str(ev),
                    "--output",
                    str(result),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(cp.returncode, 2, cp.stderr)
            self.assertEqual(
                json.loads(result.read_text())["candidate_host_monitored_source"], False
            )

    def test_trace_written_and_fsynced(self):
        from v26_clock_trace import ZERO, inspect_trace

        with TemporaryDirectory() as t:
            root = Path(t)
            trace = root / "clock.jsonl"
            clock = {
                "method": "chronyc_tracking_host_reported",
                "command_exit_code": 0,
                "leap_status": "Normal",
                "stratum": 2,
                "system_offset_seconds": 0.001,
            }
            import datetime

            obs = []
            with trace.open("xb") as fp:
                prev = ZERO
                for i in range(4):
                    x = {
                        "observed_utc": (
                            datetime.datetime(2026, 11, 15, tzinfo=datetime.UTC)
                            + datetime.timedelta(seconds=i * 20)
                        )
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "monotonic_ns": i * 20_000_000_000,
                        "boot_id_digest": "a" * 64,
                        "storage_root": "/tmp/host",
                        "free_storage_bytes": 5_000_000_000,
                        "clock": clock,
                    }
                    obs.append(x)
                    prev = module.write_trace_sample(fp, i, prev, x)
            audit = inspect_trace(
                trace,
                {"before": obs[0], "after": obs[-1]},
                mode="real_public_smoke_60s",
            )
            self.assertEqual(audit["last_trace_hash"], prev)

    def test_os_child_log_limit_is_declared(self):
        self.assertEqual(module.MAX_LOG_BYTES, 64 * 1024 * 1024)
        self.assertGreater(module.INTERVAL["24h"], module.INTERVAL["smoke"])
