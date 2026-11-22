"""Verify complete v23 release inventory; archive integrity != source truth."""

from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path

MANIFEST = "RELEASE_FILE_MANIFEST_v23.json"


def verify(root):
    root = Path(root).resolve()
    manifest = json.loads((root / MANIFEST).read_text())
    if manifest.get("release") != "v23":
        raise ValueError("Not a v23 release")
    wanted = set()
    for row in manifest["files"]:
        name = row["path"]
        if name.startswith("/") or ".." in Path(name).parts or name in wanted:
            raise ValueError("Unsafe or duplicate release member")
        wanted.add(name)
        p = root / name
        if not p.is_file() or p.is_symlink() or not p.resolve().is_relative_to(root):
            raise ValueError("Missing/unsafe release file: " + name)
        h = sha256()
        with p.open("rb") as f:
            for data in iter(lambda: f.read(1024 * 1024), b""):
                h.update(data)
        if p.stat().st_size != row["size_bytes"] or h.hexdigest() != row["sha256"]:
            raise ValueError("Size/hash mismatch: " + name)
    actual = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    }
    if actual != wanted | {MANIFEST}:
        raise ValueError("Unexpected/missing release content")
    return {
        "status": "V23_EXTRACTED_MANIFEST_MATCH",
        "source_files_sha256_checked": len(wanted),
        "zip_entries": len(wanted) + 1,
        "data_pit_external_proven": False,
        "uninterrupted_live_24h_proven": False,
        "real_trade_fill_or_alpha_proven": False,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root", type=Path)
    print(json.dumps(verify(ap.parse_args().root), sort_keys=True, indent=2))
