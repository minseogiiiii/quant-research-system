from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(
    "src/world_quant_system/order_write_certification"
)
FORBIDDEN_IMPORTS = {
    "aiohttp",
    "http.client",
    "httpx",
    "requests",
    "socket",
    "urllib.request",
}


def test_package_has_no_network_transport_imports() -> None:
    imported: set[str] = set()
    for path in PACKAGE_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.add(node.module)

    violations = {
        name
        for name in imported
        if any(
            name == forbidden or name.startswith(f"{forbidden}.")
            for forbidden in FORBIDDEN_IMPORTS
        )
    }
    assert not violations


def test_package_does_not_define_submission_capabilities() -> None:
    forbidden_names = {
        "cancel_order",
        "modify_order",
        "replace_order",
        "send_order",
        "submit_order",
    }
    discovered: set[str] = set()
    for path in PACKAGE_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                discovered.add(node.name)
    assert not forbidden_names.intersection(discovered)
