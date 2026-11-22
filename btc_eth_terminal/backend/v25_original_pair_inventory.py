"""Forensic-only discovery of older, original BTC/ETH feature/label bytes.

Discovers filenames and hashes without modifying, decompressing, relabeling or
promoting data. A discovered name is NOT proof of provider or PIT availability.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import zipfile
from hashlib import sha256
from pathlib import Path

PATTERN = re.compile(
    r"^(?P<prefix>.+)_(?P<kind>features_5m|future_labels)\.csv\.gz$", re.I
)
MAX_WALK_FILES = 100_000
MAX_ZIP_MEMBERS = 20_000
MAX_INPUT_ARCHIVES = 30


class InventoryError(ValueError):
    pass


def hash_file(path: Path):
    dig = sha256()
    with path.open("rb") as reader:
        while True:
            block = reader.read(1024 * 1024)
            if not block:
                break
            dig.update(block)
    return dig.hexdigest()


def inventory(roots, scan_zips=True):
    records, failures, archive_count = [], [], 0
    seen_files = set()
    inspected = 0
    for source in roots:
        root = Path(source)
        if root.is_symlink() or not root.exists() or not root.is_dir():
            failures.append({"path": str(root), "reason": "ROOT_MISSING_OR_SYMLINK"})
            continue
        for dirpath, subdirs, filenames in os.walk(root, followlinks=False):
            subdirs[:] = sorted(
                n for n in subdirs if not (Path(dirpath) / n).is_symlink()
            )
            for filename in sorted(filenames):
                inspected += 1
                if inspected > MAX_WALK_FILES:
                    failures.append(
                        {"path": str(root), "reason": "MAX_WALK_FILES_EXCEEDED"}
                    )
                    return finish(records, failures, inspected)
                file = Path(dirpath) / filename
                if file.is_symlink() or not file.is_file():
                    continue
                try:
                    stat = file.stat()
                except OSError:
                    failures.append({"path": str(file), "reason": "STAT_FAILED"})
                    continue
                m = PATTERN.fullmatch(filename)
                if m:
                    # Hash actual COMPRESSED bytes, not reconstructed CSV text.
                    try:
                        content_hash = hash_file(file)
                    except OSError:
                        failures.append({"path": str(file), "reason": "HASH_FAILED"})
                        continue
                    real = file.resolve()
                    if real not in seen_files:
                        records.append(
                            {
                                "location_kind": "original_compressed_file",
                                "path": str(real),
                                "member": None,
                                "sha256_compressed_bytes": content_hash,
                                "size_bytes": stat.st_size,
                                "cohort_prefix_filename_only": m.group("prefix"),
                                "kind": m.group("kind").lower(),
                                "provider_verified": False,
                                "historical_pit_verified": False,
                            }
                        )
                        seen_files.add(real)
                elif scan_zips and filename.lower().endswith(".zip"):
                    archive_count += 1
                    if archive_count > MAX_INPUT_ARCHIVES:
                        failures.append(
                            {"path": str(file), "reason": "ZIP_SCAN_LIMIT_REACHED"}
                        )
                        continue
                    try:
                        with zipfile.ZipFile(file) as zf:
                            contents = zf.infolist()
                            if len(contents) > MAX_ZIP_MEMBERS:
                                failures.append(
                                    {
                                        "path": str(file),
                                        "reason": "ZIP_MEMBER_COUNT_LIMIT",
                                    }
                                )
                                continue
                            for member in contents:
                                member_name = Path(member.filename).name
                                match = PATTERN.fullmatch(member_name)
                                if match and not member.is_dir():
                                    records.append(
                                        {
                                            "location_kind": "unextracted_zip_member_name_only",
                                            "path": str(file.resolve()),
                                            "member": member.filename,
                                            "sha256_compressed_bytes": None,
                                            "size_bytes": member.file_size,
                                            "cohort_prefix_filename_only": match.group(
                                                "prefix"
                                            ),
                                            "kind": match.group("kind").lower(),
                                            "provider_verified": False,
                                            "historical_pit_verified": False,
                                        }
                                    )
                    except (OSError, zipfile.BadZipFile):
                        failures.append({"path": str(file), "reason": "ZIP_UNREADABLE"})
    return finish(records, failures, inspected)


def finish(records, failures, inspected):
    cohorts = {}
    for record in records:
        cohort = record["cohort_prefix_filename_only"].casefold()
        cohorts.setdefault(cohort, set()).add(record["kind"])
    matched = sorted(
        k for k, kinds in cohorts.items() if kinds == {"features_5m", "future_labels"}
    )
    return {
        "schema": "v25_original_pair_inventory_v1",
        "status": "CANDIDATE_NAMES_LOCATED_NOT_PIT_VERIFIED"
        if records
        else "BLOCKED_ORIGINAL_BYTES_NOT_LOCATED",
        "files_inspected": inspected,
        "candidates": records,
        "cohort_prefixes_with_both_filenames_only": matched,
        "errors_or_limits": failures,
        "every_row_source_audit_completed": False,
        "historical_availability_verified": False,
        "research_oos_admission_allowed": False,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("roots", type=Path, nargs="+")
    p.add_argument("--no-zip-scan", action="store_true")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.output.exists() or args.output.is_symlink():
        p.error("Will not overwrite existing inventory evidence")
    result = inventory(args.roots, scan_zips=not args.no_zip_scan)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(result["status"])


if __name__ == "__main__":
    main()
