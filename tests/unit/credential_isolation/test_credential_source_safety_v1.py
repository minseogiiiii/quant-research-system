import ast
from pathlib import Path

PACKAGE = Path("src/world_quant_system/credential_isolation")
FORBIDDEN_IMPORT_ROOTS = {
    "aiohttp",
    "httpx",
    "requests",
    "socket",
    "urllib",
    "subprocess",
}
FORBIDDEN_CALL_ATTRIBUTES = {
    "connect",
    "open_connection",
    "request",
    "sendall",
    "urlopen",
}


def test_package_has_no_network_or_process_transport() -> None:
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
                assert roots.isdisjoint(FORBIDDEN_IMPORT_ROOTS), path
            if isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                assert root not in FORBIDDEN_IMPORT_ROOTS, path
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in FORBIDDEN_CALL_ATTRIBUTES, path


def test_report_model_does_not_serialize_raw_secret_fields() -> None:
    source = (
        PACKAGE / "models.py"
    ).read_text(encoding="utf-8")

    assert '"client_secret"' not in source
    assert '"access_token"' not in source
    assert '"account_id"' not in source
