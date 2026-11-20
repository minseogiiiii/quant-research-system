"""Rebuild the SHA-pinned v36 UI without overwriting frozen /research assets.

Historic frontend/dist is owned by v34 and MUST NOT be used as the Vite output.
Build in a private temporary directory; verify the exact pinned manifest and
legacy dist before installing only the accepted candidate as dist_v36.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FE = ROOT / 'frontend'
sys.path.insert(0, str(ROOT / 'backend'))


def verify(dist: Path):
    from v34_frontend_integrity import verify_build as pinned_assets
    from v36_frontend_integrity import MANIFEST, PINNED_MANIFEST_SHA256
    pinned_assets()  # Fail closed if the old /research bundle was damaged.
    bundle, manifest = pinned_assets(dist=dist, manifest_path=MANIFEST,
                                      pinned_sha256=PINNED_MANIFEST_SHA256)
    if manifest.get('mode') != 'SOURCE_ADMISSION_REQUIRED' or manifest.get('trading_enabled') is not False:
        raise RuntimeError('READ_ONLY_FRONTEND_POLICY_MISMATCH')
    print('PASS v36 isolated asset pin', hashlib.sha256(MANIFEST.read_bytes()).hexdigest())
    return bundle


def build(install: bool, promote: bool):
    # v34 legacy files and the source-pinned versioned manifest must already exist.
    old_legacy = {p.relative_to(FE / 'dist').as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in (FE / 'dist').rglob('*') if p.is_file()}
    from v34_frontend_integrity import verify_build as verify_historical
    verify_historical()
    if install:
        subprocess.run(['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], cwd=FE, check=True)
    staging = FE / '.dist_v36_candidate_unpromoted'
    if staging.exists():
        shutil.rmtree(staging)
    try:
        subprocess.run([str(FE / 'node_modules/.bin/tsc'), '--noEmit'], cwd=FE, check=True)
        subprocess.run([str(FE / 'node_modules/.bin/vite'), 'build', '--outDir', staging.name], cwd=FE, check=True)
        if promote:
            verify(staging)  # Must match reviewed source-pinned Q6 asset hashes.
            published = FE / 'dist_v36'
            backup = FE / '.dist_v36_previous'
            if backup.exists():
                shutil.rmtree(backup)
            published.rename(backup)
            try:
                staging.rename(published)
            except Exception:
                backup.rename(published)
                raise
            shutil.rmtree(backup)
        else:
            files = {p.relative_to(staging).as_posix(): {
                'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                for p in staging.rglob('*') if p.is_file()}
            candidate = {'schema':'v34_funnest_build_integrity_v1',
                'release':'v36_engineering_candidate_read_only',
                'mode':'SOURCE_ADMISSION_REQUIRED','trading_enabled':False,
                'files':dict(sorted(files.items()))}
            out = ROOT / 'verification/v36_candidate_manifest_UNPROMOTED.json'
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(candidate, indent=2, sort_keys=True)+'\n')
            print('Candidate only, review before repinning:', out)
        after_legacy = {p.relative_to(FE / 'dist').as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in (FE / 'dist').rglob('*') if p.is_file()}
        if old_legacy != after_legacy:
            raise RuntimeError('HISTORICAL_FRONTEND_MUTATED')
        if promote:
            verify(FE / 'dist_v36')
            print('PASS v36 isolated build; frozen historical frontend/dist preserved')
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-existing', action='store_true', help='verify pinned dist_v36 and historical dist only')
    parser.add_argument('--offline-build', action='store_true', help='compile using already installed pinned dependencies')
    parser.add_argument('--promote', action='store_true', help='require pinned manifest match, then replace dist_v36 only')
    parser.add_argument('--from-v36-dist', action='store_true', help='compatibility alias for --verify-existing')
    args = parser.parse_args()
    if args.verify_existing or args.from_v36_dist:
        verify(FE / 'dist_v36')
    else:
        build(install=not args.offline_build, promote=args.promote)


if __name__ == '__main__':
    main()
