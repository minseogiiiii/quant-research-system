#!/usr/bin/env python3
"""Run immutable private Q7 historical Node fixtures without publishing market data.

The exact frozen release archive must be obtained from a private, trusted source.
Raw fixture bytes are never printed or uploaded; temporary extraction is deleted.
This is a source-contract qualification, NOT a production or trading approval.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path

ARCHIVE_SHA256 = "0e49a7e2d7692452304ad5f555688182b4b74c1803c0b63f5094a4d526ca6b15"
PREFIX = "btc_eth_unified_terminal_v36_candidate/"
FIXTURES = {
    "V34_L2_PROJECTION": (
        "backend/data/v34_l2_depth/projection.json",
        62789,
        "9c93c9a53ea4c03937198886ede3df71c03af03691349c245d0d8506246b88de",
    ),
    "Q5_REAL_STATE_JSONL": (
        "verification/v36_3_r5_q6/archive_state_228.jsonl",
        1214230,
        "6830f6f2054b80fcc2ddddf763bc6d4593d057ab31c5c21b0b91f6c646ffa079",
    ),
    "Q4_REAL_STATE_JSON": (
        "verification/v36_3_r5_q6/archive_final_snapshots.json",
        11688,
        "25decc8f75efea8499aa6fa0245c779e3d3a753f6751749da1d2c4078e90322a",
    ),
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_zip(archive: Path) -> zipfile.ZipFile:
    if not archive.is_file() or archive.stat().st_size != 61656622:
        raise ValueError("Q13_RELEASE_MISSING_OR_SIZE_MISMATCH")
    if sha256(archive) != ARCHIVE_SHA256:
        raise ValueError("Q13_RELEASE_SHA256_MISMATCH")
    z = zipfile.ZipFile(archive)
    seen: set[str] = set()
    entries = z.infolist()
    for entry in entries:
        name = entry.filename
        p = Path(name)
        if p.is_absolute() or ".." in p.parts or not name.startswith(PREFIX):
            z.close()
            raise ValueError("Q13_UNSAFE_ZIP_PATH")
        if stat.S_IFMT(entry.external_attr >> 16) == stat.S_IFLNK:
            z.close()
            raise ValueError("Q13_ZIP_SYMLINK_FORBIDDEN")
        if not entry.is_dir():
            if name in seen:
                z.close()
                raise ValueError("Q13_DUPLICATE_ZIP_MEMBER")
            seen.add(name)
    if sum(e.file_size for e in entries) > 1_000_000_000:
        z.close()
        raise ValueError("Q13_EXCESSIVE_DECOMPRESSED_SIZE")
    return z


def extract_subset(archive: Path, root: Path) -> dict[str, Path]:
    paths = {key: root / rel for key, (rel, _, _) in FIXTURES.items()}
    with verify_zip(archive) as z:
        required = {PREFIX + rel for rel, _, _ in FIXTURES.values()}
        if not required.issubset(z.namelist()):
            raise ValueError("Q13_HISTORICAL_FIXTURE_MISSING")
        for entry in z.infolist():
            if entry.is_dir():
                continue
            rel = entry.filename[len(PREFIX):]
            if not rel.startswith("frontend/") and entry.filename not in required:
                continue
            if rel.startswith("frontend/node_modules/"):
                raise ValueError("Q13_EMBEDDED_DEPENDENCIES_DISALLOWED")
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(entry) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    for key, (_rel, expected_size, expected_sha) in FIXTURES.items():
        f = paths[key]
        if f.stat().st_size != expected_size or sha256(f) != expected_sha:
            raise ValueError(f"Q13_FIXTURE_INTEGRITY_FAILURE_{key}")
        if key == "Q5_REAL_STATE_JSONL":
            with f.open(encoding="utf-8") as fh:
                products = {json.loads(line).get("product") for line in fh if line.strip()}
        elif key == "Q4_REAL_STATE_JSON":
            products = set(json.loads(f.read_text(encoding="utf-8")))
        else:
            products = set(json.loads(f.read_text(encoding="utf-8"))["products"])
        if not {"BTC-USD", "ETH-USD"}.issubset(products):
            raise ValueError(f"Q13_FIXTURE_MISSING_PRODUCTS_{key}")
    return paths


def compare_checkout(root: Path, checkout: Path) -> None:
    """Verify archived frontend source and tests are exactly the staged GitHub files."""
    base = checkout / "btc_eth_terminal/frontend"
    for part in ("src", "tests"):
        archived = root / "frontend" / part
        staged = base / part
        if not staged.is_dir():
            raise ValueError("Q13_STAGED_SOURCE_NOT_FOUND")
        archive_files = {p.relative_to(archived) for p in archived.rglob("*") if p.is_file()}
        staged_files = {p.relative_to(staged) for p in staged.rglob("*") if p.is_file()}
        if archive_files != staged_files:
            raise ValueError("Q13_FRONTEND_SOURCE_FILESET_MISMATCH")
        for name in archive_files:
            if sha256(archived / name) != sha256(staged / name):
                raise ValueError("Q13_FRONTEND_STAGED_SOURCE_MISMATCH")


def run(archive: Path, checkout: Path | None) -> dict[str, object]:
    if shutil.which("node") is None:
        raise ValueError("Q13_NODE_NOT_AVAILABLE")
    version = subprocess.check_output(["node", "--version"], text=True).strip()
    if not version.startswith("v22."):
        raise ValueError("Q13_NODE_22_REQUIRED")
    with tempfile.TemporaryDirectory(prefix="q13_private_") as tmp:
        root = Path(tmp)
        env = dict(os.environ)
        env.update({key: str(path) for key, path in extract_subset(archive, root).items()})
        if checkout is not None:
            compare_checkout(root, checkout)
        result = subprocess.run(
            [
                "node",
                "--experimental-strip-types",
                "--test",
                *sorted(str(x) for x in (root / "frontend/tests").glob("*.test.mjs")),
            ],
            cwd=root / "frontend",
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        metrics = {}
        for key in ("tests", "pass", "fail", "skipped"):
            match = re.search(r"(?m)^# " + key + r" (\d+)$", result.stdout)
            if match is None:
                raise ValueError("Q13_INCOMPLETE_NODE_TAP")
            metrics[key] = int(match.group(1))
        if result.returncode or metrics != {"tests": 31, "pass": 31, "fail": 0, "skipped": 0}:
            raise ValueError("Q13_NODE_ZERO_SKIP_GATE_FAILED")
        return {
            "schema": "q13_private_historical_fixture_gate_v1",
            "release_zip_sha256": ARCHIVE_SHA256,
            "fixture_sha256": {key: item[2] for key, item in FIXTURES.items()},
            "tests": 31,
            "passed": 31,
            "failed": 0,
            "skipped": 0,
            "frontend_source_compared_to_checkout": checkout is not None,
            "private_fixture_embedded_in_report": False,
            "real_ordering_enabled": False,
            "release_approval": False,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-zip", required=True, type=Path)
    parser.add_argument("--checkout-root", type=Path)
    args = parser.parse_args()
    report = run(args.release_zip, args.checkout_root)
    print("PASS_Q13_PRIVATE_NODE_31_31_ZERO_SKIP")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
