"""SHA and inventory verification of extracted v25 release; not market truth."""
from __future__ import annotations
import argparse
from hashlib import sha256
import json
from pathlib import Path

MANIFEST = 'RELEASE_FILE_MANIFEST_v25.json'


def verify(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Unsafe release root')
    root = root.resolve()
    report = json.loads((root / MANIFEST).read_text())
    if report.get('release') != 'v25':
        raise ValueError('Not v25')
    expected = set()
    for record in report['files']:
        name = record['path']
        if (not isinstance(name, str) or name.startswith('/') or
                '..' in Path(name).parts or name in expected):
            raise ValueError('Duplicate or unsafe path')
        expected.add(name)
        p = root / name
        if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(root):
            raise ValueError('Missing or escaped file: ' + name)
        with p.open('rb') as stream:
            h = sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                h.update(block)
        if h.hexdigest() != record['sha256'] or p.stat().st_size != record['size_bytes']:
            raise ValueError('Manifest mismatch: ' + name)
    found = {p.relative_to(root).as_posix() for p in root.rglob('*')
             if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
    if found != expected | {MANIFEST}:
        raise ValueError('Unlisted or missing files')
    return {'status': 'V25_EXTRACTED_RELEASE_SHA_MATCH',
            'file_hashes_verified': len(expected), 'zip_expected_members': len(expected)+1,
            'real_live_capture_verified': False,
            'actual_24h_verified': False, 'independent_alpha_verified': False}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('root', type=Path)
    print(json.dumps(verify(ap.parse_args().root), indent=2, sort_keys=True))
