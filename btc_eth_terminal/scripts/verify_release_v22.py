"""Verify exact v22 extracted file inventory and hashes; no market authenticity claim."""

from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path


def verify(root):
    root = Path(root).resolve()
    manifest = json.loads((root / "RELEASE_FILE_MANIFEST_v22.json").read_text())
    if manifest.get("release") != "v22":
        raise ValueError("Wrong release")
    entries = manifest["files"]
    wanted = set()
    for item in entries:
        name = item["path"]
        if name.startswith("/") or ".." in Path(name).parts or name in wanted:
            raise ValueError("Unsafe or duplicate path")
        wanted.add(name)
        p = root / name
        if not p.is_file() or p.is_symlink() or not p.resolve().is_relative_to(root):
            raise ValueError("Absent/symlinked/escaped entry: " + name)
        b = sha256()
        with p.open("rb") as f:
            for data in iter(lambda: f.read(1024 * 1024), b""):
                b.update(data)
        if item["sha256"] != b.hexdigest() or item["size_bytes"] != p.stat().st_size:
            raise ValueError("SHA/size failure: " + name)
    actual = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    }
    if actual != wanted | {"RELEASE_FILE_MANIFEST_v22.json"}:
        raise ValueError(
            "Missing or extra paths: "
            + str(
                list(
                    actual.symmetric_difference(
                        wanted | {"RELEASE_FILE_MANIFEST_v22.json"}
                    )
                )[:10]
            )
        )
    return {
        "status": "EXACT_V22_FILES_VERIFIED",
        "files_sha256_checked": len(entries),
        "zip_entries_expected": len(entries) + 1,
        "original_market_source_authenticity_proven": False,
        "original_feature_label_historical_availability_proven": False,
        "actual_24h_continuity_proven": False,
        "live_alpha_verified": False,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("root", type=Path)
    a = p.parse_args()
    print(json.dumps(verify(a.root), indent=2))
