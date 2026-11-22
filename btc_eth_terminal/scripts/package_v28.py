"""Deterministic v28 release builder based on the exact frozen v27 ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

PARENT_SHA = "b70b612c0a1d980c95b7af33d9e28b5cac1056e60dcf4d7c534fdb411c36a8f0"
MANIFEST = "RELEASE_FILE_MANIFEST_v28.json"
SKIP = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git"}


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def entries(root):
    root = Path(root).resolve()
    for f in sorted(root.rglob("*")):
        rel = f.relative_to(root)
        if any(p in SKIP for p in rel.parts) or f.suffix in (".pyc", ".pid"):
            continue
        if f.is_symlink():
            raise ValueError("Symlink: " + str(rel))
        if f.is_file() and f.name != MANIFEST:
            if not f.resolve().is_relative_to(root):
                raise ValueError("Escaped path")
            yield rel.as_posix(), f


def zipped(output, paths):
    if output.exists() or output.is_symlink():
        raise ValueError("Refusing overwrite")
    with ZipFile(
        output, "w", compression=ZIP_DEFLATED, compresslevel=6, allowZip64=True
    ) as z:
        for name, p in sorted(paths):
            if name.startswith("/") or any(
                x in ("", ".", "..") for x in name.split("/")
            ):
                raise ValueError("Unsafe ZIP name")
            info = ZipInfo(name, date_time=(2026, 11, 15, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with p.open("rb") as reader, z.open(info, "w", force_zip64=True) as writer:
                for block in iter(lambda: reader.read(1024 * 1024), b""):
                    writer.write(block)
    with ZipFile(output) as zipin:
        if zipin.testzip() is not None:
            raise ValueError("Bad ZIP CRC")


def build(root, parent, full, patch):
    if sha(parent) != PARENT_SHA:
        raise ValueError("Unrecognized v27 source ZIP")
    with ZipFile(parent) as z:
        old = {
            x.filename: hashlib.sha256(z.read(x.filename)).hexdigest()
            for x in z.infolist()
            if not x.is_dir()
        }
    raw = list(entries(root))
    lookup = dict(raw)
    expected = [
        "backend/v28_cohort_ledger.py",
        "tests/test_v28_cohort_ledger.py",
        "V28_SOURCE_COHORT_LEDGER.json",
        "V28_VALIDATION_REPORT.md",
        "V28_RUNBOOK.md",
        "V28_DEVELOPMENT_CHECKLIST.md",
        "CHANGELOG_v28.md",
        "evidence/v27_github_real_60s/github_workflow_artifact.zip",
    ]
    if any(f not in lookup for f in expected):
        raise ValueError("Incomplete release")
    ledger = json.loads(lookup["V28_SOURCE_COHORT_LEDGER.json"].read_text())
    if (
        ledger["cohort_count"] != 11
        or ledger["complete_chunk_sha_verified_cohorts"] != 11
        or ledger["double_replayed_cohort_count"] != 3
        or ledger["quarantined_cohorts"] != 1
        or ledger["overlapping_cohort_pairs"] != 2
        or ledger["independent_blind_oos_eligible_cohorts"] != 0
        or ledger["executable_alpha_confirmed"] is not False
        or ledger["live_orders"] != 0
    ):
        raise ValueError("Incorrect frozen source audit")
    for name, p in raw:
        if name.startswith(
            ("backend/data/", "evidence/v27_github_real_60s/")
        ) and old.get(name) != sha(p):
            raise ValueError("Research source mutated: " + name)
    m = {
        "release": "v28",
        "parent_release": "v27",
        "parent_zip_sha256": PARENT_SHA,
        "scope": "PIT_AND_OOS_BLOCKED_SOURCE_COHORT_OVERLAP_AUDIT",
        "cohort_count": 11,
        "verified_raw_archive_bytes": ledger["sha256_verified_archive_bytes_total"],
        "double_replayed_critical_cohorts": 3,
        "quarantined_cohorts": 1,
        "temporal_overlapping_pairs": 2,
        "new_real_capture_in_v28": False,
        "v27_real_capture_retained": True,
        "continuous_24h_verified": False,
        "original_historical_feature_label_recovered": False,
        "blind_oos_alpha_verified": False,
        "real_orders": 0,
        "files": [
            {"path": name, "size_bytes": p.stat().st_size, "sha256": sha(p)}
            for name, p in raw
        ],
    }
    out_manifest = Path(root) / MANIFEST
    if out_manifest.exists() or out_manifest.is_symlink():
        raise ValueError("Manifest exists")
    with out_manifest.open("x") as f:
        json.dump(m, f, sort_keys=True, indent=2)
        f.write("\n")
    paths = raw + [(MANIFEST, out_manifest)]
    diff = [(name, p) for name, p in paths if old.get(name) != sha(p)]
    zipped(full, paths)
    zipped(patch, diff)
    return {
        "full_zip_sha256": sha(full),
        "full_zip_bytes": full.stat().st_size,
        "full_zip_entries": len(paths),
        "full_manifest_file_count": len(raw),
        "patch_zip_sha256": sha(patch),
        "patch_zip_bytes": patch.stat().st_size,
        "patch_entries": len(diff),
        "parent_zip_sha256": PARENT_SHA,
        "preserves_original_v27_capture": True,
        "source_archive_bytes_immutable": True,
        "real_orders": 0,
        "executable_alpha_verified": False,
        "changed_paths": [name for name, _ in diff],
    }


def main():
    p = argparse.ArgumentParser()
    for k in ("root", "parent", "full", "patch", "report"):
        p.add_argument("--" + k, type=Path, required=True)
    args = p.parse_args()
    report = build(args.root, args.parent, args.full, args.patch)
    if args.report.exists() or args.report.is_symlink():
        raise ValueError("Report exists")
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps({k: v for k, v in report.items() if k != "changed_paths"}, indent=2)
    )


if __name__ == "__main__":
    main()
