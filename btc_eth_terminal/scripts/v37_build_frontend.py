"""Build Q7 from reviewed source into versioned-only frozen dist_v37.

Must not overwrite Q6 dist_v36, historic dist, dist_v35_1 or prior pins.
Candidate manifest is generated on a separate path for human SHA review.
"""

import argparse
import json
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FE = ROOT / "frontend"
sys.path.insert(0, str(ROOT / "backend"))
from v34_frontend_integrity import verify_build as verify_old
from v36_frontend_integrity import v36_asset
from v37_frontend_integrity import DIST, MANIFEST, PINNED_MANIFEST_SHA256


def digest(p):
    return sha256(p.read_bytes()).hexdigest()


def baseline_hashes():
    frozen = {}
    for name in ("dist", "dist_v35_1", "dist_v36"):
        d = FE / name
        frozen[name] = {
            str(f.relative_to(d)): digest(f) for f in d.rglob("*") if f.is_file()
        }
    return frozen


def build(promote=False):
    verify_old()
    v36_asset("/research/v36")
    before = baseline_hashes()
    staging = FE / ".dist_v37_candidate"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        subprocess.run(
            [str(FE / "node_modules/.bin/tsc"), "--noEmit"], cwd=FE, check=True
        )
        subprocess.run(
            [str(FE / "node_modules/.bin/vite"), "build", "--outDir", staging.name],
            cwd=FE,
            check=True,
        )
        files = {
            f.relative_to(staging).as_posix(): {
                "bytes": f.stat().st_size,
                "sha256": digest(f),
            }
            for f in staging.rglob("*")
            if f.is_file()
        }
        proposal = {
            "schema": "v34_funnest_build_integrity_v1",
            "release": "v37_factor_lab_engineering_candidate_read_only",
            "mode": "SOURCE_ADMISSION_REQUIRED",
            "trading_enabled": False,
            "files": dict(sorted(files.items())),
        }
        path = ROOT / "verification/v37_candidate_manifest_UNPROMOTED.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(proposal, indent=2, sort_keys=True) + "\n")
        if promote:
            # Never rewrite manifest merely to force a passing comparison.
            if (
                digest(path) != PINNED_MANIFEST_SHA256
                or path.read_bytes() != MANIFEST.read_bytes()
            ):
                raise RuntimeError("CANDIDATE_NOT_REVIEWED_OR_REPINNED")
            from v34_frontend_integrity import verify_build

            verify_build(
                dist=staging,
                manifest_path=MANIFEST,
                pinned_sha256=PINNED_MANIFEST_SHA256,
            )
            if DIST.exists():
                # The caller must explicitly archive the previously pinned output.
                raise RuntimeError("V37_DISTRIBUTION_ALREADY_EXISTS")
            staging.rename(DIST)
        assert before == baseline_hashes(), "FROZEN_HISTORICAL_ASSETS_CHANGED"
        print(
            "Q7",
            "PROMOTED_REVIEWED_VERSIONED_DIST" if promote else "CANDIDATE_ONLY",
            digest(path),
        )
    finally:
        if staging.exists():
            shutil.rmtree(staging)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--promote", action="store_true")
    a = p.parse_args()
    build(a.promote)
