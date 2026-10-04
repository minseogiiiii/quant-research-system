import os
import subprocess
import sys
import sysconfig
from pathlib import Path

EXPECTED_LINES = (
    "Execution mode: MOCK",
    "Broker provider: NONE",
    "Live trading: DISABLED",
)


def sanitized_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def assert_successful_application_run(
    result: subprocess.CompletedProcess[str],
) -> None:
    assert result.returncode == 0, result.stderr
    for expected_line in EXPECTED_LINES:
        assert expected_line in result.stdout


def test_module_entrypoint(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "world_quant_system"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=sanitized_environment(),
    )

    assert_successful_application_run(result)


def test_console_script_entrypoint(tmp_path: Path) -> None:
    extension = ".exe" if os.name == "nt" else ""
    script = Path(sysconfig.get_path("scripts")) / (
        f"world-quant-system{extension}"
    )

    assert script.is_file(), f"Console script was not installed: {script}"

    result = subprocess.run(
        [str(script)],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=sanitized_environment(),
    )

    assert_successful_application_run(result)
