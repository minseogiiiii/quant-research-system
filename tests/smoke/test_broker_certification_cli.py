from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _environment() -> dict[str, str]:
    environment = dict(os.environ)
    source = str(ROOT / "src")
    current = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = source if not current else f"{source}:{current}"
    return environment


def test_cli_show_contract_is_read_only() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "world_quant_system.broker_certification.cli",
            "show-contract",
        ],
        cwd=ROOT,
        env=_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Network transport: DISABLED" in result.stdout
    assert "Write operations: DISABLED" in result.stdout
    assert "GET /api/v1/accounts" in result.stdout
    assert "POST" not in result.stdout


def test_cli_certifies_capture_bundle(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "world_quant_system.broker_certification.cli",
            "certify-captures",
            "--policy",
            str(ROOT / "examples/toss_read_only_certification_policy.example.json"),
            "--captures",
            str(ROOT / "examples/toss_read_only_capture_bundle.example.json"),
            "--certified-at",
            "2026-08-03T21:30:00Z",
            "--json-output",
            str(output),
        ],
        cwd=ROOT,
        env=_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Status:                   PASS" in result.stdout
    assert output.is_file()
