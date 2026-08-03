import subprocess
import sys


def test_cli_show_contract() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "world_quant_system.credential_isolation.cli",
            "show-contract",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Credential Boundary & Token Isolation v1" in result.stdout
    assert "External network:          DISABLED" in result.stdout
    assert "Broker writes:             DISABLED" in result.stdout
