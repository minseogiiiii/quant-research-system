"""Independently verify all v27 release files and expected evidence statuses."""

from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path

MANIFEST = "RELEASE_FILE_MANIFEST_v27.json"


def checksum(path):
    h = sha256()
    with path.open("rb") as reader:
        for block in iter(lambda: reader.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def check(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Unsafe root")
    root = root.resolve()
    mf = root / MANIFEST
    if mf.is_symlink() or not mf.is_file():
        raise ValueError("Missing manifest")
    data = json.loads(mf.read_text())
    if (
        data.get("release") != "v27"
        or data.get("parent_release") != "v26"
        or data.get("new_actual_public_websocket_60s") is not True
        or data.get("new_source_v26_host_clock_attested") is not False
        or data.get("actual_continuous_24h_verified") is not False
        or data.get("independent_blind_oos_alpha_verified") is not False
        or data.get("live_orders_sent") != 0
    ):
        raise ValueError("Misrepresented release status")
    files = {}
    for item in data.get("files", []):
        name = item["path"]
        if (
            not isinstance(name, str)
            or not name
            or name.startswith("/")
            or "\\" in name
            or any(seg in ("", ".", "..") for seg in name.split("/"))
            or name in files
        ):
            raise ValueError("Unsafe manifest path")
        path = root / name
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(root)
            or path.stat().st_size != item["size_bytes"]
            or checksum(path) != item["sha256"]
        ):
            raise ValueError("File missing or modified: " + name)
        files[name] = item["sha256"]
    excluded = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    found = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix != ".pyc"
        and not any(part in excluded for part in p.relative_to(root).parts)
    }
    if found != set(files) | {MANIFEST}:
        raise ValueError("Unlisted or missing files")
    source = json.loads((root / "V27_SOURCE_EVIDENCE_REPORT.json").read_text())
    if (
        source.get("status")
        != "REAL_PUBLIC_60S_SOURCE_ARCHIVED_INTERNALLY_DOUBLE_REPLAYED"
        or source.get("source_manifest_sha256")
        != "a68a147322575274f455c02fcf5888df1d903cc7a4b21d6e5f8b4e71abc4e517"
        or source.get("research_blind_oos_eligible") is not False
    ):
        raise ValueError("Wrong v27 source evidence status")
    return {
        "release": "v27",
        "status": "SHA256_MANIFEST_ENTIRE_PACKAGE_MATCH",
        "files_checked": len(files),
        "expected_zip_files": len(files) + 1,
        "real_public_60s_source_included": True,
        "v26_host_clock_verified": False,
        "real_24h_verified": False,
        "executable_alpha_verified": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    print(json.dumps(check(parser.parse_args().root), indent=2, sort_keys=True))
