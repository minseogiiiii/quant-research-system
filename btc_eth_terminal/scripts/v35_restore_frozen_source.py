"""Restore missing *code only* from the verified v34.1 ZIP into the existing staging tree.

Never imports market archives or touches existing files. This is NOT a v35 service
implementation or a complete deployment. Review conflicts before any release.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

EXPECTED_ZIP_SHA256 = "8304562514657d3a6405255d595a8ce7e1f223eef2f0f15f157b8f18d6dacec8"
EXPECTED_MANIFEST = "RELEASE_FILE_MANIFEST_v34_1.json"
REQUIRED_V35 = ("backend/v35_live_state.py", "backend/v35_live_api.py")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_file(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if len(parts) != 2:
        return False
    folder, basename = parts
    return (
        folder == "backend"
        and basename.endswith(".py")
        or folder == "tests"
        and basename.startswith("test_")
        and basename.endswith(".py")
        or folder == "scripts"
        and basename.endswith(".py")
    )


def validated_archive(archive: Path) -> dict[str, bytes]:
    if not archive.is_file() or archive.is_symlink():
        raise ValueError("Missing or symlinked frozen archive")
    with archive.open("rb") as src:
        digest = hashlib.file_digest(src, "sha256").hexdigest()
    if digest != EXPECTED_ZIP_SHA256:
        raise ValueError("Frozen v34.1 ZIP digest mismatch")
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        if len(names) != len(set(names)) or EXPECTED_MANIFEST not in names:
            raise ValueError("Missing manifest or duplicate archive names")
        for info in z.infolist():
            p = PurePosixPath(info.filename)
            if (
                p.is_absolute()
                or ".." in p.parts
                or not p.parts
                or "\\" in info.filename
                or (info.external_attr >> 16) & 0o170000 == stat.S_IFLNK
            ):
                raise ValueError("Unsafe frozen archive path or symlink")
        manifest = json.loads(z.read(EXPECTED_MANIFEST))
        if (
            manifest.get("release") != "v34.1"
            or manifest.get("trading_enabled") is not False
            or manifest.get("historical_only") is not True
        ):
            raise ValueError("Unexpected frozen release semantics")
        recorded = {item["path"]: item for item in manifest["files"]}
        if set(recorded) != set(names) - {EXPECTED_MANIFEST}:
            raise ValueError("Release inventory differs from ZIP members")
        selected: dict[str, bytes] = {}
        for path in names:
            if not source_file(path):
                continue
            raw = z.read(path)
            meta = recorded[path]
            if len(raw) != meta["size_bytes"] or sha256_bytes(raw) != meta["sha256"]:
                raise ValueError(f"Manifest validation failed for {path}")
            selected[path] = raw
    if not all(
        p in selected for p in ("backend/v34_l2_api.py", "backend/v34_l2_depth.py")
    ):
        raise ValueError("Frozen v34 source graph incomplete")
    return selected


def reconcile(archive: Path, target: Path, apply: bool = False) -> dict:
    """Never overwrites changed files, public data, or existing frontend assets."""
    files = validated_archive(archive)
    if not target.is_dir() or target.is_symlink():
        raise ValueError(
            "Destination staging root must be an existing non-symlink directory"
        )
    before = {"missing": [], "identical": [], "conflicts": []}
    for rel, data in sorted(files.items()):
        out = target.joinpath(*PurePosixPath(rel).parts)
        # Reject symlinked ancestors even during dry-run.
        if any(
            p.is_symlink()
            for p in (out, *out.parents)
            if p == target or target in p.parents
        ):
            raise ValueError(f"Symlink in destination path: {rel}")
        if not out.exists():
            before["missing"].append(rel)
        elif out.is_file() and sha256_bytes(out.read_bytes()) == sha256_bytes(data):
            before["identical"].append(rel)
        else:
            before["conflicts"].append(rel)
    written = []
    if apply:
        # Conflicts are always preserved, and reported as unresolved.
        for rel in before["missing"]:
            out = target.joinpath(*PurePosixPath(rel).parts)
            out.parent.mkdir(parents=True, exist_ok=True)
            if any(
                p.is_symlink()
                for p in (out, *out.parents)
                if p == target or target in p.parents
            ):
                raise ValueError(f"Symlink inserted in destination path: {rel}")
            # Exclusive creation means a concurrent change cannot be overwritten.
            with out.open("xb") as f:
                f.write(files[rel])
            written.append(rel)
    missing_v35 = [p for p in REQUIRED_V35 if not (target / p).is_file()]
    return {
        "schema": "v35_v34_1_code_reconcile_v1",
        "baseline_sha256": EXPECTED_ZIP_SHA256,
        "dry_run": not apply,
        "candidate_root": str(target.resolve()),
        "selected_code_files": len(files),
        "missing_before": before["missing"],
        "identical": before["identical"],
        "conflicts_preserved": before["conflicts"],
        "written": written,
        "v35_files_still_missing": missing_v35,
        "raw_archives_imported": False,
        "frontend_modified": False,
        "release_ready": False,
        "status": "REQUIRES_REVIEW"
        if before["conflicts"] or missing_v35
        else "BASE_CODE_PRESENT_ONLY",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen-zip", required=True, type=Path)
    parser.add_argument(
        "--target",
        required=True,
        type=Path,
        help="Existing btc_eth_terminal staging directory",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Copy only missing Python source files, never overwrite",
    )
    parser.add_argument(
        "--report", type=Path, help="Optional machine-readable JSON report"
    )
    opts = parser.parse_args(argv)
    try:
        report = reconcile(opts.frozen_zip, opts.target, opts.apply)
    except (
        ValueError,
        OSError,
        KeyError,
        zipfile.BadZipFile,
        json.JSONDecodeError,
    ) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, ensure_ascii=False))
        return 2
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    print(encoded)
    if opts.report:
        opts.report.write_text(encoded, encoding="utf-8")
    return 1 if report["status"] == "REQUIRES_REVIEW" else 0


if __name__ == "__main__":
    sys.exit(main())
