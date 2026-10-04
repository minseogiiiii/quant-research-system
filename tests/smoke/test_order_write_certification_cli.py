from __future__ import annotations

import os
import subprocess
import sysconfig
from pathlib import Path


def test_order_write_certification_cli_contract() -> None:
    extension = ".exe" if os.name == "nt" else ""
    script = (
        Path(sysconfig.get_path("scripts"))
        / f"wqs-order-write-certify{extension}"
    )
    assert script.is_file(), f"Console script was not installed: {script}"

    result = subprocess.run(
        [str(script), "show-contract"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "POST /api/v1/orders" in result.stdout
    assert "Credentials:              DISABLED" in result.stdout
    assert "Broker writes:            DISABLED" in result.stdout
