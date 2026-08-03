from __future__ import annotations

import subprocess
import sys


def test_module_cli_contract() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "world_quant_system.disabled_write_transport.cli",
            "show-contract",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Disabled Write Transport Integration v1" in result.stdout
    assert "Broker writes:            DISABLED" in result.stdout
    assert "Final state:              TRANSPORT_BLOCKED" in result.stdout
