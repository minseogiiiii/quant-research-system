"""Verify packaged release source and real provider evidence by SHA-256.

Never authenticates exchange origin, legal venue access, latency or alpha. v12/v13
are verified against complete byte inventories, not only a selected subset.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    d = hashlib.sha256()
    with path.open('rb') as stream:
        for buf in iter(lambda: stream.read(1024*1024), b''):
            d.update(buf)
    return d.hexdigest()


def verify(release='v12') -> dict:
    if release not in ('v11','v12','v13','v14','v15','v16','v17'):
        raise ValueError('Unsupported named release')
    manifest_path = ROOT / ('RELEASE_FILE_MANIFEST_' + release + '.json')
    obj = json.loads(manifest_path.read_text())
    files = obj.get('files')
    if obj.get('release') != release or not isinstance(files,list) or not files:
        raise ValueError('Invalid release file catalog')
    known=set()
    for item in files:
        rel=item.get('path')
        if (not isinstance(rel,str) or not rel or rel in known or
                Path(rel).is_absolute() or '..' in Path(rel).parts):
            raise ValueError('Unsafe/duplicate relative file path')
        known.add(rel)
        p=ROOT / rel
        if (p.is_symlink() or not p.is_file() or
                not p.resolve().is_relative_to(ROOT.resolve())):
            raise ValueError('Missing/symlinked/escaped release source file')
        if p.stat().st_size != item.get('bytes') or sha256(p) != item.get('sha256'):
            raise ValueError('Size/SHA mismatch in release source: '+rel)
    if release in ('v12','v13','v14','v15','v16','v17'):
        disk={str(p.relative_to(ROOT)) for p in ROOT.rglob('*')
              if p.is_file() and '__pycache__' not in p.parts
              and p.suffix != '.pyc' and p.name != ('RELEASE_FILE_MANIFEST_' + release + '.json')}
        if disk != known:
            raise ValueError('Unlisted/missing release entries: '+str(sorted(disk ^ known)[:15]))
    return {'release':release,'file_count':len(known),
            'status':'EXACT_PACKAGED_SOURCE_BYTES_VERIFIED',
            'independent_market_truth_verified':False,
            'full_browser_build_verified':False,
            'live_order_fills_verified':False,
            'executable_alpha_verified':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--release', choices=['v11','v12','v13','v14','v15','v16','v17'],default='v17')
    args=p.parse_args()
    try:
        result=verify(args.release)
        code=0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result={'status':'RELEASE_FILE_INTEGRITY_FAILED','error':str(exc)[:500]}
        code=1
    print(json.dumps(result,indent=2,sort_keys=True))
    raise SystemExit(code)


if __name__=='__main__':main()
