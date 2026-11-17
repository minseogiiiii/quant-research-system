"""Reproducible v36 build verification without touching frozen v34/v35 UI.

Use --verify-existing in CI immediately after pinned `npm ci` and `npm run build`.
Use --offline-build when pinned dependencies are already installed locally.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
FE=ROOT/'frontend'

def verify(from_v36_dist:bool=False):
    built=FE/('dist_v36' if from_v36_dist else 'dist')
    manifest_file=FE/'v36_dist_integrity.json'
    manifest=json.loads(manifest_file.read_bytes())
    assert manifest['mode']=='SOURCE_ADMISSION_REQUIRED'
    assert manifest['trading_enabled'] is False
    files=manifest['files']
    expected=set(files)
    actual={str(p.relative_to(built)) for p in built.rglob('*') if p.is_file()}
    if actual!=expected:raise RuntimeError('BUNDLE_FILE_LIST_MISMATCH: '+repr((actual,expected)))
    for name,item in files.items():
        if name.startswith('/') or '..' in Path(name).parts:raise RuntimeError('UNSAFE_ASSET_NAME')
        data=(built/name).read_bytes()
        if len(data)!=item['bytes'] or sha256(data).hexdigest()!=item['sha256']:
            raise RuntimeError('V36_ASSET_HASH_MISMATCH:'+name)
    if not from_v36_dist:
        if FE.joinpath('dist_v36').exists():shutil.rmtree(FE/'dist_v36')
        shutil.copytree(FE/'dist',FE/'dist_v36')
    print('PASS V36 SHA_PINNED_FRONTEND '+sha256(manifest_file.read_bytes()).hexdigest())

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-existing',action='store_true')
    parser.add_argument('--offline-build',action='store_true')
    parser.add_argument('--from-v36-dist',action='store_true')
    args=parser.parse_args()
    if not args.verify_existing:
        if not args.offline_build:
            subprocess.run(['npm','ci','--ignore-scripts','--no-audit','--no-fund'],cwd=FE,check=True)
        subprocess.run(['npm','run','build'],cwd=FE,check=True)
    verify(from_v36_dist=args.from_v36_dist)
if __name__=='__main__':main()
