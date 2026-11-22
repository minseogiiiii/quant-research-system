"""Build immutable standalone v20 ZIP and v19-A-to-v20 patch; audit ZIP CRC."""

import argparse
import json
import zipfile
from hashlib import sha256
from pathlib import Path

MANIFEST = "RELEASE_FILE_MANIFEST_v20.json"


def hash_bytes(b):
    return sha256(b).hexdigest()


def iter_sources(root):
    for file in sorted(root.rglob("*")):
        if not file.is_file() or "__pycache__" in file.parts or file.suffix == ".pyc":
            continue
        if file.relative_to(root).as_posix() == MANIFEST:
            continue
        if file.is_symlink() or not file.resolve().is_relative_to(root.resolve()):
            raise ValueError("Unsafe source path " + str(file))
        yield file.relative_to(root).as_posix(), file


def build(root, original, full, patch):
    sources = list(iter_sources(root))
    manifest = {
        "release": "v20",
        "format": "original+source_inventory_SHA256",
        "files": [
            {
                "path": name,
                "size_bytes": file.stat().st_size,
                "sha256": hash_bytes(file.read_bytes()),
            }
            for name, file in sources
        ],
        "original_venue_and_user_fill_attestation": False,
        "actual_24h_continuity_verified": False,
        "live_orders_placed": False,
    }
    (root / MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with zipfile.ZipFile(original) as src:
        originals = {
            name: hash_bytes(src.read(name))
            for name in src.namelist()
            if not name.endswith("/")
        }
    changed = [
        (name, file)
        for name, file in sources
        if originals.get(name) != hash_bytes(file.read_bytes())
    ]
    changed.append((MANIFEST, root / MANIFEST))
    for path, items in (
        (full, sources + [(MANIFEST, root / MANIFEST)]),
        (patch, changed),
    ):
        with zipfile.ZipFile(
            path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as z:
            for name, file in items:
                z.write(file, arcname=name)
        with zipfile.ZipFile(path) as z:
            bad = z.testzip()
            if bad is not None:
                raise ValueError("ZIP CRC failure: " + bad)
    return {
        "files_full": len(sources) + 1,
        "files_patch": len(changed),
        "full_sha256": hash_bytes(full.read_bytes()),
        "patch_sha256": hash_bytes(patch.read_bytes()),
        "full_bytes": full.stat().st_size,
        "patch_bytes": patch.stat().st_size,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--original", type=Path, required=True)
    p.add_argument("--full", type=Path, required=True)
    p.add_argument("--patch", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    args = p.parse_args()
    r = build(args.root, args.original, args.full, args.patch)
    args.report.write_text(json.dumps(r, indent=2, sort_keys=True) + "\n")
    print(json.dumps(r, indent=2))


if __name__ == "__main__":
    main()
