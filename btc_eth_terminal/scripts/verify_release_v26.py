(
    'Verify extracted v26 package'
    ' manifest and every file, wi'
    'thout trusting ZIP metadata.'
)

from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path

MANIFEST = "RELEASE_FILE_MANIFEST_v26.json"


def check(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Unsafe root")
    root = root.resolve()
    raw = root / MANIFEST
    if raw.is_symlink() or not raw.is_file():
        raise ValueError("Missing release manifest")
    obj = json.loads(raw.read_text())
    if obj.get("release") != "v26" or obj.get("parent_release") != "v25":
        raise ValueError("Wrong release lineage")
    expected = {}
    for record in obj["files"]:
        name = record["path"]
        if (
            not isinstance(name, str)
            or not name
            or name.startswith("/")
            or "\\" in name
            or any(x in ("", ".", "..") for x in name.split("/"))
            or name in expected
        ):
            raise ValueError("Unsafe or duplicate source name")
        path = root / name
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(root)
        ):
            raise ValueError("Missing or unsafe file: " + name)
        digest = sha256()
        with path.open("rb") as src:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                digest.update(chunk)
        if (
            digest.hexdigest() != record["sha256"]
            or path.stat().st_size != record["size_bytes"]
        ):
            raise ValueError("Source changed: " + name)
        expected[name] = record["sha256"]
    found = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.suffix != ".pyc" and "__pycache__" not in path.parts
    }
    if found != set(expected) | {MANIFEST}:
        raise ValueError("Unlisted or missing release files")
    return {
        "release": "v26",
        "status": "SHA256_MANIFEST_ENTIRE_PACKAGE_MATCH",
        "files_checked": len(expected),
        "zip_expected_members": len(expected) + 1,
        "new_live_capture_verified": False,
        "independent_alpha_verified": False,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    print(json.dumps(check(p.parse_args().root), sort_keys=True, indent=2))
