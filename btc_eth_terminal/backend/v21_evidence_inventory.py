"""Non-mutating discovery of possible original BTC/ETH feature/label bytes.

Searches only explicitly supplied roots. ZIP inspection lists names, never extracts
or trusts content automatically. Discovery is not source authenticity or PIT proof.
"""

from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path
from zipfile import BadZipFile, ZipFile

SUFFIXES = ("_features_5m.csv.gz", "_future_labels.csv.gz")
MAX_FILES = 100000


def digest(path):
    h = sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inventory(roots, scan_zips=True):
    found = []
    archives = []
    scanned = []
    examined = 0
    skipped = []
    for supplied in roots:
        root = Path(supplied)
        if root.is_symlink() or not root.exists() or not root.is_dir():
            skipped.append(
                {"path": str(root), "reason": "NOT_ACCESSIBLE_OR_SYMLINK_ROOT"}
            )
            continue
        scanned.append(str(root.resolve()))
        for path in sorted(root.rglob("*")):
            if examined >= MAX_FILES:
                raise ValueError("File inventory cap reached; split roots")
            if path.is_symlink():
                skipped.append({"path": str(path), "reason": "SYMLINK_SKIPPED"})
                continue
            if not path.is_file():
                continue
            examined += 1
            if path.name.endswith(SUFFIXES):
                found.append(
                    {
                        "location": "DIRECT_FILE",
                        "path": str(path),
                        "size_bytes": path.stat().st_size,
                        "sha256": digest(path),
                        "authenticated_origin": False,
                        "historical_availability_attested": False,
                    }
                )
            if scan_zips and path.suffix.lower() == ".zip":
                try:
                    with ZipFile(path) as z:
                        matches = [
                            i
                            for i in z.infolist()
                            if not i.is_dir() and i.filename.endswith(SUFFIXES)
                        ]
                        if matches:
                            archives.append(
                                {
                                    "archive": str(path),
                                    "archive_sha256": digest(path),
                                    "members": [
                                        {
                                            "name": m.filename,
                                            "uncompressed_bytes": m.file_size,
                                        }
                                        for m in matches
                                    ],
                                    "member_bytes_not_authenticated_or_extracted": True,
                                }
                            )
                except (BadZipFile, OSError) as exc:
                    skipped.append(
                        {
                            "path": str(path),
                            "reason": "ZIP_UNREADABLE:" + type(exc).__name__,
                        }
                    )
    return {
        "status": (
            "CANDIDATES_FOUND_REQUIRE_SOURCE_AUTHENTICATION"
            if found or archives
            else "BLOCKED_ORIGINAL_FEATURE_LABEL_BYTES_NOT_FOUND_IN_SEARCHED_ROOTS"
        ),
        "roots_searched": scanned,
        "items_examined": examined,
        "direct_candidates": found,
        "zip_member_candidates": archives,
        "skipped": skipped,
        "actual_original_pair_source_provenance_verified": False,
        "point_in_time_availability_verified": False,
        "eligible_for_blind_oos": False,
        "scope_note": (
            'Only listed accessible roots'
            ' scanned; absent elsewhere r'
            'emains unknown.'
        ),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, nargs="+", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    result = inventory(args.root)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(
        result["status"],
        "direct",
        len(result["direct_candidates"]),
        "zip",
        len(result["zip_member_candidates"]),
    )


if __name__ == "__main__":
    main()
