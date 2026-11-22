"""Build deterministic v27 standalone ZIP and v26 SHA-pinned incremental ZIP."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

PARENT_SHA = 'f4fd2511323141e8377625af2d106c3e6ab251cf082516dfde66112c260f299e'
MANIFEST = 'RELEASE_FILE_MANIFEST_v27.json'
IGNORE_DIRS = {'__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'}


def checksum(path):
    h = sha256()
    with Path(path).open('rb') as stream:
        for buf in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(buf)
    return h.hexdigest()


def sources(root):
    root = Path(root).resolve()
    for item in sorted(root.rglob('*')):
        rel = item.relative_to(root)
        if any(part in IGNORE_DIRS for part in rel.parts) or item.suffix in ('.pyc', '.pid'):
            continue
        if item.is_symlink():
            raise ValueError('Symlink found: ' + str(rel))
        if item.is_file() and item.name != MANIFEST:
            if not item.resolve().is_relative_to(root):
                raise ValueError('Source escapes root')
            yield rel.as_posix(), item


def zip_write(target, files):
    if target.exists() or target.is_symlink():
        raise ValueError('Refusing to overwrite target ZIP')
    with ZipFile(target, 'w', compression=ZIP_DEFLATED, compresslevel=6, allowZip64=True) as out:
        for name, file in sorted(files):
            if name.startswith('/') or any(segment in ('', '.', '..') for segment in name.split('/')):
                raise ValueError('Unsafe member name')
            info = ZipInfo(name, date_time=(2026, 11, 15, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with file.open('rb') as reader, out.open(info, 'w', force_zip64=True) as writer:
                for buf in iter(lambda: reader.read(1024*1024), b''):
                    writer.write(buf)
    with ZipFile(target) as z:
        if z.testzip() is not None:
            raise ValueError('ZIP CRC failed')


def create(root, parent, full, patch):
    if checksum(parent) != PARENT_SHA:
        raise ValueError('Not the exact frozen v26 parent ZIP')
    with ZipFile(parent) as old:
        old_entries = {info.filename: sha256(old.read(info.filename)).hexdigest()
                       for info in old.infolist() if not info.is_dir()}
    paths = list(sources(root))
    file_index = {name: file for name, file in paths}
    required = [
        'V27_SOURCE_EVIDENCE_REPORT.json',
        'V27_CAPACITY_OBSERVATION.json',
        'V27_SOURCE_COHORT_REGISTRY.json',
        'backend/v27_github_public_smoke_audit.py',
        'tests/test_v27_github_source.py',
        'evidence/v27_github_real_60s/github_workflow_artifact.zip',
    ]
    if any(f not in file_index for f in required):
        raise ValueError('Missing required v27 artifact')
    if checksum(file_index['evidence/v27_github_real_60s/github_workflow_artifact.zip']) != (
        '8720e127b506f9a38d04113f012c56fc4c26115b67ad941fa2734287bd87d55f'
    ):
        raise ValueError('Wrong actual GitHub artifact')
    status = json.loads(file_index['V27_SOURCE_EVIDENCE_REPORT.json'].read_text())
    if (status.get('status') != 'REAL_PUBLIC_60S_SOURCE_ARCHIVED_INTERNALLY_DOUBLE_REPLAYED'
            or status.get('research_blind_oos_eligible') is not False
            or status.get('v26_host_monitored_clock_evidence_available') is not False):
        raise ValueError('Incorrect research promotion status')
    # Ensure original research data files are completely unchanged in bytes.
    for name, file in paths:
        if name.startswith('backend/data/') and old_entries.get(name) != checksum(file):
            raise ValueError('Original historical data file mutated: ' + name)
    description = {'release': 'v27', 'parent_release': 'v26',
                   'parent_zip_sha256': PARENT_SHA,
                   'scope': 'NEW_REAL_PUBLIC_60S_INTERNAL_REPLAY_ONLY',
                   'github_workflow_run_id': 43696932946,
                   'new_actual_public_websocket_60s': True,
                   'new_public_source_double_replayed': True,
                   'new_source_v26_host_clock_attested': False,
                   'actual_continuous_24h_verified': False,
                   'original_historical_feature_label_bytes_recovered': False,
                   'independent_blind_oos_alpha_verified': False,
                   'live_broker_access_enabled': False,
                   'live_orders_sent': 0,
                   'files': [{'path': name, 'size_bytes': file.stat().st_size,
                              'sha256': checksum(file)} for name, file in paths]}
    manifest_path = Path(root) / MANIFEST
    if manifest_path.exists():
        raise ValueError('v27 manifest already exists')
    manifest_path.write_text(json.dumps(description, sort_keys=True, indent=2) + '\n')
    all_files = paths + [(MANIFEST, manifest_path)]
    changes = [(name, file) for name, file in all_files
               if old_entries.get(name) != checksum(file)]
    zip_write(full, all_files)
    zip_write(patch, changes)
    return {'release': 'v27', 'parent_zip_sha256': PARENT_SHA,
            'full_zip_sha256': checksum(full), 'full_zip_bytes': full.stat().st_size,
            'full_zip_entries': len(all_files),
            'full_manifest_files': len(paths),
            'patch_zip_sha256': checksum(patch), 'patch_zip_bytes': patch.stat().st_size,
            'patch_entries': len(changes), 'changed_paths': [a for a, _ in changes],
            'new_real_public_source_included': True,
            'legacy_data_source_bytes_preserved': True,
            'real_24h_verified': False, 'executable_alpha_verified': False,
            'real_orders_sent': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'parent', 'full', 'patch', 'report'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    result = create(args.root, args.parent, args.full, args.patch)
    if args.report.exists():
        raise ValueError('Refusing report overwrite')
    args.report.write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'changed_paths'}, indent=2))


if __name__ == '__main__':
    main()
