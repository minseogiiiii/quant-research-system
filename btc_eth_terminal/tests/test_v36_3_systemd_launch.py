"""Systemd must start an unexecutable 0644 GitHub-launch script safely."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LinuxSystemdContract(unittest.TestCase):
    def test_deployment_unit_uses_explicit_shell_for_git_0644_script(self):
        unit = (ROOT / "deployment/v36_2_btceth_live.service").read_text()
        self.assertIn(
            "ExecStart=/bin/sh /opt/btc-eth-terminal/scripts/v36_2_durable_launch.sh",
            unit,
        )
        self.assertIn("User=btceth", unit)
        self.assertIn("NoNewPrivileges=yes", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("ReadWritePaths=/var/lib/btceth-live", unit)
        self.assertIn("Restart=always", unit)
        self.assertNotIn(
            "ExecStart=/opt/btc-eth-terminal/scripts/v36_2_durable_launch.sh", unit
        )

    def test_non_executable_shell_source_works(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "mode0644.sh"
            path.write_text("#!/bin/sh\nprintf verified-shell-invocation\n")
            path.chmod(0o644)
            self.assertFalse(os.access(path, os.X_OK))
            p = subprocess.run(
                ["/bin/sh", str(path)],
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            )
            self.assertEqual(p.stdout, "verified-shell-invocation")

    def test_operator_script_syntax_and_hard_storage_limits_remain(self):
        script = ROOT / "scripts/v36_2_durable_launch.sh"
        subprocess.run(["/bin/sh", "-n", str(script)], timeout=3, check=True)
        text = script.read_text()
        for v in (
            "--durable-operator-mode",
            "--duration 86400",
            "--raw-budget-bytes 40000000000",
            "--archive-budget-bytes 55000000000",
            "--disk-reserve-bytes 10737418240",
        ):
            self.assertIn(v, text)


if __name__ == "__main__":
    unittest.main()
