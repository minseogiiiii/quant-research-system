"""v29 immutable full and v28-pinned patch distribution. No source mutation."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

PARENT_SHA = "bcb47312215c649bf3f828860897c14810a6bd37b022b98788bee003c74c1aeb"
MANIFEST = "RELEASE_FILE_MANIFEST_v29.json"
SKIP = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git"}
SOURCES = ("backend/data/", "evidence/v27_github_real_60s/")


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def entries(root):
    root = Path(root).resolve()
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(x in SKIP for x in rel.parts) or p.suffix in (".pyc", ".pid"):
            continue
        if p.is_symlink():
            raise ValueError("Refusing symlink: " + str(rel))
        if p.is_file() and p.name != MANIFEST:
            if not p.resolve().is_relative_to(root):
                raise ValueError("Escaped file")
            yield rel.as_posix(), p


def writezip(path, records):
    if path.exists() or path.is_symlink():
        raise ValueError("Output already exists")
    with ZipFile(
        path, "w", compression=ZIP_DEFLATED, compresslevel=6, allowZip64=True
    ) as z:
        for name, p in sorted(records):
            if name.startswith("/") or any(
                x in ("", ".", "..") for x in name.split("/")
            ):
                raise ValueError("Unsafe ZIP path")
            i = ZipInfo(name, (2026, 11, 15, 0, 0, 0))
            i.compress_type = ZIP_DEFLATED
            i.external_attr = 0o644 << 16
            with p.open("rb") as reader, z.open(i, "w", force_zip64=True) as writer:
                for b in iter(lambda: reader.read(1024 * 1024), b""):
                    writer.write(b)
    with ZipFile(path) as z:
        if z.testzip() is not None:
            raise ValueError("ZIP CRC failure")


def build(root, parent, full, patch):
    if sha(parent) != PARENT_SHA:
        raise ValueError("Unexpected v28 baseline SHA")
    with ZipFile(parent) as z:
        old = {
            x.filename: hashlib.sha256(z.read(x.filename)).hexdigest()
            for x in z.infolist()
            if not x.is_dir()
        }
    raw = list(entries(root))
    lookup = dict(raw)
    expected = [
        "backend/v29_capture_verdict.py",
        "backend/v29_ready_window.py",
        "scripts/v29_verified_capture.py",
        "tests/test_v29_capture_verdict.py",
        "tests/test_v29_ready_window.py",
        "ops/v29-btceth-24h.service",
        "ops/V29_HOST_DEPLOYMENT.md",
        "CHANGELOG_v29.md",
        "V29_RUNBOOK.md",
        "V29_DEVELOPMENT_CHECKLIST.md",
        "V29_VALIDATION_REPORT.md",
        "V29_LOCAL_EXECUTION_AUDIT.json",
        "V29_FROZEN_SOURCE_REPLAY_VERIFICATION.json",
        "evidence/v29_local_fail_closed/V29_EXECUTE_REQUEST_VERDICT.json",
    ]
    if any(p not in lookup for p in expected):
        raise ValueError("Missing v29 release file")
    for name, p in raw:
        if (
            name.startswith(SOURCES) or name == "V28_SOURCE_COHORT_LEDGER.json"
        ) and old.get(name) != sha(p):
            raise ValueError("Original market source or cohort ledger changed: " + name)
    for name in old:
        if name.startswith(SOURCES) and name not in lookup:
            raise ValueError("Original market source deleted: " + name)
    preflight = json.loads(lookup["V29_LOCAL_EXECUTION_AUDIT.json"].read_text())
    if (
        preflight["host_local_24h_preflight_ready"] is not False
        or preflight["new_coinbase_source_created_by_v29"] is not False
        or preflight["real_orders_sent"] != 0
    ):
        raise ValueError("Misleading 24h run result")
    manifest = {
        "release": "v29",
        "parent_release": "v28",
        "parent_zip_sha256": PARENT_SHA,
        "scope": "FAIL_CLOSED_HOST_24H_CAPTURE_SUPERVISION_AND_SOURCE_PROTECTION",
        "latest_24h_capture_verified": False,
        "new_actual_capture_this_release": False,
        "historical_original_feature_label_recovered": False,
        "independent_blind_oos_approved": False,
        "confirmed_executable_alpha": False,
        "trading_enabled": False,
        "real_orders": 0,
        "files": [
            {"path": name, "sha256": sha(p), "size_bytes": p.stat().st_size}
            for name, p in raw
        ],
    }
    location = Path(root) / MANIFEST
    if location.exists() or location.is_symlink():
        raise ValueError("Manifest exists")
    location.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    all_records = raw + [(MANIFEST, location)]
    changed = [(n, p) for n, p in all_records if old.get(n) != sha(p)]
    writezip(full, all_records)
    writezip(patch, changed)
    return {
        "full_zip_sha256": sha(full),
        "full_zip_bytes": full.stat().st_size,
        "full_zip_file_count": len(all_records),
        "manifest_data_file_count": len(raw),
        "patch_zip_sha256": sha(patch),
        "patch_zip_bytes": patch.stat().st_size,
        "patch_file_count": len(changed),
        "parent_zip_sha256": PARENT_SHA,
        "preserved_all_original_market_files": True,
        "new_24h_capture_verified": False,
        "real_orders": 0,
        "confirmed_alpha": False,
        "changed_paths": [name for name, _ in changed],
    }


def main():
    p = argparse.ArgumentParser()
    for name in ("root", "parent", "full", "patch", "report"):
        p.add_argument("--" + name, type=Path, required=True)
    a = p.parse_args()
    result = build(a.root, a.parent, a.full, a.patch)
    if a.report.exists() or a.report.is_symlink():
        raise ValueError("Existing report")
    a.report.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(
        json.dumps({k: v for k, v in result.items() if k != "changed_paths"}, indent=2)
    )


if __name__ == "__main__":
    main()
