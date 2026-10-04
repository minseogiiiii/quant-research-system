from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

from world_quant_system.disabled_write_transport.reporting import (
    AtomicJsonDisabledWriteTransportReportWriter,
)
from world_quant_system.disabled_write_transport.simulation import (
    build_simulation_report,
)


def _project_root() -> Path:
    for root in (Path.cwd(), *Path.cwd().parents):
        if (root / "src/world_quant_system").is_dir():
            return root
    raise FileNotFoundError("Project source root was not found.")


def test_source_has_no_network_or_submission_capability() -> None:
    root = _project_root() / "src/world_quant_system/disabled_write_transport"
    forbidden_imports = {
        "aiohttp",
        "http.client",
        "httpx",
        "requests",
        "socket",
        "subprocess",
        "urllib.request",
        "websockets",
    }
    forbidden_functions = {
        "cancel_order",
        "modify_order",
        "replace_order",
        "submit_order",
    }
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(
                    alias.name in forbidden_imports for alias in node.names
                )
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                assert node.module not in forbidden_imports
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.name not in forbidden_functions


def test_atomic_writer_replaces_complete_json(tmp_path: Path) -> None:
    report = asyncio.run(build_simulation_report())
    output = tmp_path / "transport-report.json"
    AtomicJsonDisabledWriteTransportReportWriter(output).write(report)
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["report_digest"] == report.report_digest
    assert document["broker_write_count"] == 0
    assert document["network_call_count"] == 0
    assert not list(tmp_path.glob("*.tmp"))
