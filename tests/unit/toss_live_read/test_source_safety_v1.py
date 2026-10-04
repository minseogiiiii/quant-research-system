from __future__ import annotations

import ast
from pathlib import Path


def test_live_read_package_has_no_broker_write_calls() -> None:
    root = Path("src/world_quant_system/toss_live_read")
    forbidden_attributes = {"submit_order", "modify_order", "cancel_order"}
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in forbidden_attributes


def test_network_import_is_isolated_to_transport_module() -> None:
    root = Path("src/world_quant_system/toss_live_read")
    for path in root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if path.name == "http_transport.py":
            assert "import http.client" in source
        else:
            assert "import http.client" not in source
            assert "import socket" not in source
            assert "import requests" not in source
            assert "import httpx" not in source
