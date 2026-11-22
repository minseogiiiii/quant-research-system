"""Reproducible v24 synthetic-only full and exact-v23-parent patch ZIPs."""

from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

PARENT = "6b3fa3b74c25ba4e5b3e4c04f80d0aab31e23faa7a5b4799139e7167b5fae845"
MANIFEST = "RELEASE_FILE_MANIFEST_v24.json"


def sha(p):
    h = sha256()
    with Path(p).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def paths(root):
    for p in sorted(root.rglob("*")):
        if (
            not p.is_file()
            or "__pycache__" in p.parts
            or p.suffix == ".pyc"
            or p.name == MANIFEST
            or p.suffix == ".pid"
        ):
            continue
        if p.is_symlink() or not p.resolve().is_relative_to(root.resolve()):
            raise ValueError("Unsafe source file")
        yield p.relative_to(root).as_posix(), p


def archive(path, items):
    with ZipFile(
        path, "w", compression=ZIP_DEFLATED, compresslevel=6, allowZip64=True
    ) as z:
        for name, p in sorted(items):
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError("Unsafe archive path")
            zi = ZipInfo(name, date_time=(2026, 11, 15, 0, 0, 0))
            zi.compress_type = ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            with (
                Path(p).open("rb") as source,
                z.open(zi, "w", force_zip64=True) as target,
            ):
                for data in iter(lambda: source.read(1024 * 1024), b""):
                    target.write(data)
    with ZipFile(path) as z:
        if z.testzip() is not None:
            raise ValueError("Archive CRC verification failed")


def build(root, parent, full, patch):
    root = Path(root)
    if sha(parent) != PARENT:
        raise ValueError("v23 parent archive SHA mismatch")
    with ZipFile(parent) as z:
        prior = {
            name: sha256(z.read(name)).hexdigest()
            for name in z.namelist()
            if not name.endswith("/")
        }
    files = list(paths(root))
    report = {
        "release": "v24",
        "parent_release": "v23",
        "parent_zip_sha256": PARENT,
        "source_manifest_sha256": "08255d79f0a7f081921f370feca2e92686cde3dffbb406eadc23e918d43e73d7",
        "fixture_only_paper_oms": True,
        "production_paper_oms_approved": False,
        "actual_original_feature_label_pair_verified": False,
        "actual_24h_continuous_capture_verified": False,
        "real_trading_orders_sent": False,
        "independent_oos_alpha_verified": False,
        "files": [
            {"path": name, "size_bytes": p.stat().st_size, "sha256": sha(p)}
            for name, p in files
        ],
    }
    p = root / MANIFEST
    p.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    complete = files + [(MANIFEST, p)]
    changed = [(n, p) for n, p in complete if prior.get(n) != sha(p)]
    archive(full, complete)
    archive(patch, changed)
    return {
        "release": "v24",
        "status": "LOCAL_SYNTHETIC_ENGINEERING_FIXTURE_NOT_P3_NO_ALPHA",
        "full_zip_sha256": sha(full),
        "full_zip_bytes": Path(full).stat().st_size,
        "full_zip_entries": len(complete),
        "manifest_files_verified_on_packaging": len(files),
        "patch_zip_sha256": sha(patch),
        "patch_zip_bytes": Path(patch).stat().st_size,
        "patch_entries": len(changed),
        "changed_paths": [n for n, _ in changed],
        "parent_zip_sha256": PARENT,
        "actual_24h_capture_verified": False,
        "original_feature_label_pit_verified": False,
        "independent_executable_alpha_verified": False,
        "fixture_only_paper_oms": True,
        "production_paper_oms_approved": False,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "parent", "full", "patch", "report"):
        ap.add_argument("--" + name, type=Path, required=True)
    args = ap.parse_args()
    result = build(args.root, args.parent, args.full, args.patch)
    args.report.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(
        json.dumps({k: v for k, v in result.items() if k != "changed_paths"}, indent=2)
    )


if __name__ == "__main__":
    main()
