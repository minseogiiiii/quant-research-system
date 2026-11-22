"""Deterministic v25 standalone + v24-parent-locked incremental patch ZIPs."""

from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

PARENT = "72bf32f162dc72d6827104cf10dc4b66f6955ec3b32e1e77e0746d69735d6720"
MANIFEST = "RELEASE_FILE_MANIFEST_v25.json"


def digest_stream(stream):
    h = sha256()
    for item in iter(lambda: stream.read(1024 * 1024), b""):
        h.update(item)
    return h.hexdigest()


def digest(path):
    with Path(path).open("rb") as stream:
        return digest_stream(stream)


def safe_files(root):
    for p in sorted(root.rglob("*")):
        if (
            not p.is_file()
            or p.suffix == ".pyc"
            or "__pycache__" in p.parts
            or p.name == MANIFEST
            or p.suffix == ".pid"
        ):
            continue
        if p.is_symlink() or not p.resolve().is_relative_to(root.resolve()):
            raise ValueError("Unsafe source file: " + str(p))
        yield p.relative_to(root).as_posix(), p


def write_archive(target, files):
    if target.exists():
        raise ValueError("Refusing overwrite of archive: " + str(target))
    with ZipFile(
        target, "w", compression=ZIP_DEFLATED, compresslevel=6, allowZip64=True
    ) as archive:
        for name, path in sorted(files):
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError("Unsafe ZIP path: " + name)
            info = ZipInfo(name, date_time=(2026, 11, 15, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with (
                path.open("rb") as input_stream,
                archive.open(info, "w", force_zip64=True) as output,
            ):
                for block in iter(lambda: input_stream.read(1024 * 1024), b""):
                    output.write(block)
    with ZipFile(target) as archive:
        if archive.testzip() is not None:
            raise ValueError("ZIP CRC failed")


def build(root, parent, full, patch):
    if digest(parent) != PARENT:
        raise ValueError("Wrong v24 parent ZIP SHA-256")
    with ZipFile(parent) as archive:
        prior = {
            name: sha256(archive.read(name)).hexdigest()
            for name in archive.namelist()
            if not name.endswith("/")
        }
    source = list(safe_files(root))
    desc = {
        "release": "v25",
        "parent_release": "v24",
        "parent_zip_sha256": PARENT,
        "source_manifest_sha256": (
            '08255d79f0a7f081921f370feca2'
            'e92686cde3dffbb406eadc23e918'
            'd43e73d7'
        ),
        "scope": "HOST_REPORTED_SOURCE_EVIDENCE_GATE_NOT_REAL_LIVE_VERIFIED",
        "actual_24h_capture_verified": False,
        "original_user_feature_label_pit_verified": False,
        "independent_oos_alpha_verified": False,
        "real_orders_placed": False,
        "files": [
            {"path": name, "size_bytes": path.stat().st_size, "sha256": digest(path)}
            for name, path in source
        ],
    }
    manifest_path = root / MANIFEST
    manifest_path.write_text(json.dumps(desc, sort_keys=True, indent=2) + "\n")
    all_files = source + [(MANIFEST, manifest_path)]
    changes = [
        (name, path) for name, path in all_files if prior.get(name) != digest(path)
    ]
    write_archive(full, all_files)
    write_archive(patch, changes)
    return {
        "release": "v25",
        "parent_zip_sha256": PARENT,
        "full_zip_sha256": digest(full),
        "full_zip_bytes": full.stat().st_size,
        "full_zip_members": len(all_files),
        "manifest_listed_files": len(source),
        "patch_zip_sha256": digest(patch),
        "patch_zip_bytes": patch.stat().st_size,
        "patch_members": len(changes),
        "changed_file_paths": [n for n, _ in changes],
        "real_24h_capture_verified": False,
        "original_pit_source_verified": False,
        "executable_alpha_verified": False,
        "real_orders_sent": 0,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for field in ("root", "parent", "full", "patch", "report"):
        ap.add_argument("--" + field, type=Path, required=True)
    opts = ap.parse_args()
    result = build(opts.root, opts.parent, opts.full, opts.patch)
    if opts.report.exists():
        raise SystemExit("Report already exists")
    opts.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "changed_file_paths"}, indent=2
        )
    )


if __name__ == "__main__":
    main()
