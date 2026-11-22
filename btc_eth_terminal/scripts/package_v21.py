"""Deterministic v21 standalone and patch ZIP, from the verified v20 parent.

No network access, trading, original source edits or blind OOS changes.
"""

from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

FROZEN_PARENT_SHA = "d115802bcbadb6cac6a94eabcfdf1a560532040bb4fec9003d2b7e6d9cd74f5c"
MANIFEST = "RELEASE_FILE_MANIFEST_v21.json"


def digest(path):
    h = sha256()
    with path.open("rb") as f:
        for piece in iter(lambda: f.read(1024 * 1024), b""):
            h.update(piece)
    return h.hexdigest()


def sources(root):
    for p in sorted(root.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or p.suffix == ".pyc":
            continue
        if p.is_symlink() or not p.resolve().is_relative_to(root.resolve()):
            raise ValueError("Unsafe release entry: " + str(p))
        if p.name == MANIFEST:
            continue
        yield (p.relative_to(root).as_posix(), p)


def write_zip(path, entries):
    with ZipFile(
        path, "w", compression=ZIP_DEFLATED, compresslevel=6, allowZip64=True
    ) as z:
        for name, p in sorted(entries):
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError("Unsafe archive path")
            # Stable metadata for reproducible ZIP bytes on same Python/zlib.
            info = ZipInfo(name, date_time=(2026, 11, 14, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with p.open("rb") as f, z.open(info, "w", force_zip64=True) as out:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    out.write(chunk)
    with ZipFile(path) as z:
        if z.testzip() is not None:
            raise ValueError("ZIP CRC failed")


def build(root, parent, full, patch):
    root = Path(root)
    if digest(parent) != FROZEN_PARENT_SHA:
        raise ValueError("Original v20 ZIP SHA mismatch")
    with ZipFile(parent) as z:
        prev = {
            n: sha256(z.read(n)).hexdigest()
            for n in z.namelist()
            if not n.endswith("/")
        }
    files = list(sources(root))
    catalog = {
        n: {"path": n, "size_bytes": p.stat().st_size, "sha256": digest(p)}
        for n, p in files
    }
    manifest = {
        "release": "v21",
        "format": "immutable_original_plus_derived_v21_SHA256",
        "parent_release": "v20",
        "parent_zip_sha256": FROZEN_PARENT_SHA,
        "source_manifest_sha256": (
            '08255d79f0a7f081921f370feca2'
            'e92686cde3dffbb406eadc23e918'
            'd43e73d7'
        ),
        "original_user_pair_audited": False,
        "real_24h_capture_verified": False,
        "actual_market_order_fills_verified": False,
        "executable_alpha_verified": False,
        "orders_sent": False,
        "files": list(catalog.values()),
    }
    mp = root / MANIFEST
    mp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    complete = files + [(MANIFEST, mp)]
    delta = [(n, p) for n, p in complete if prev.get(n) != digest(p)]
    write_zip(full, complete)
    write_zip(patch, delta)
    return {
        "parent_zip_sha256": FROZEN_PARENT_SHA,
        "full_zip_sha256": digest(full),
        "full_zip_bytes": full.stat().st_size,
        "full_entries": len(complete),
        "manifest_file_entries": len(files),
        "patch_zip_sha256": digest(patch),
        "patch_zip_bytes": patch.stat().st_size,
        "patch_entries": len(delta),
        "changed_or_new_paths": [n for n, _ in delta],
        "no_actual_24h_claim": True,
        "no_user_original_pair_claim": True,
    }


if __name__ == "__main__":
    pa = argparse.ArgumentParser(description=__doc__)
    pa.add_argument("--root", type=Path, required=True)
    pa.add_argument("--parent", type=Path, required=True)
    pa.add_argument("--full", type=Path, required=True)
    pa.add_argument("--patch", type=Path, required=True)
    pa.add_argument("--report", type=Path, required=True)
    a = pa.parse_args()
    result = build(a.root, a.parent, a.full, a.patch)
    a.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "changed_or_new_paths"}, indent=2
        )
    )
