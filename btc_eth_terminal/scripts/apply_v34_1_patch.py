#!/usr/bin/env python3
"""Apply an SHA-locked v34->v34.1 patch with an explicit audited deletion list.

The original parent ZIP is never modified. No file from arbitrary external ZIP
is written until parent, patch inventory and target manifest hashes pass.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import zipfile

PARENT_SHA='03760467a0ec53b4a5f5ef85600332fe2b5476e98d7f57be8e536cd74bccf870'
PATCH_META='V34_1_PATCH_METADATA.json'
TARGET_MANIFEST='RELEASE_FILE_MANIFEST_v34_1.json'


def sha(raw:bytes)->str:return hashlib.sha256(raw).hexdigest()


def safe_name(name:str)->str:
    p=PurePosixPath(name)
    if (not name or name.startswith('/') or '\\' in name or
       '..' in p.parts or any(x in ('','.') for x in name.split('/')) or p.as_posix()!=name):
        raise ValueError('Unsafe ZIP path')
    return name


def collect(z:zipfile.ZipFile)->dict[str,bytes]:
    result={}
    for entry in z.infolist():
        name=safe_name(entry.filename)
        if name in result or entry.is_dir() or ((entry.external_attr >> 16)&0o170000)==stat.S_IFLNK:
            raise ValueError('Duplicate, directory or symlink ZIP member')
        if entry.file_size>75_000_000 or len(result)>=1500:
            raise ValueError('Unbounded ZIP member or file count')
        result[name]=z.read(entry)
    return result


def reconstruct(parent:Path, patch:Path)->dict[str,bytes]:
    if sha(parent.read_bytes())!=PARENT_SHA:raise ValueError('Wrong parent ZIP digest')
    with zipfile.ZipFile(parent) as z:initial=collect(z)
    with zipfile.ZipFile(patch) as z:changes=collect(z)
    if PATCH_META not in changes:raise ValueError('Missing patch metadata')
    meta=json.loads(changes.pop(PATCH_META))
    if (meta.get('schema')!='v34_1_exact_parent_patch_v1' or
        meta.get('parent_zip_sha256')!=PARENT_SHA or
        not isinstance(meta.get('deleted_paths'),dict) or
        set(changes)!=set(meta.get('changed_paths',{}))):
        raise ValueError('Invalid exact-parent patch contract')
    for name,digest in meta['deleted_paths'].items():
        safe_name(name)
        if name not in initial or sha(initial[name])!=digest or name in changes:
            raise ValueError('Patch deletion does not match parent')
        del initial[name]
    for name,raw in changes.items():
        if sha(raw)!=meta['changed_paths'][name]:
            raise ValueError('Patch changed-file hash mismatch')
        initial[name]=raw
    if TARGET_MANIFEST not in initial:raise ValueError('Missing v34 release manifest')
    manifest=json.loads(initial[TARGET_MANIFEST])
    expected={x['path']:x for x in manifest['files']}
    if len(expected)!=len(manifest['files']) or set(expected)!=set(initial)-{TARGET_MANIFEST}:
        raise ValueError('Target file inventory mismatch')
    for name,meta in expected.items():
        if sha(initial[name])!=meta['sha256'] or len(initial[name])!=meta['size_bytes']:
            raise ValueError('Target hash mismatch: '+name)
    return initial


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--parent',required=True,type=Path)
    p.add_argument('--patch',required=True,type=Path)
    p.add_argument('--output',required=True,type=Path)
    a=p.parse_args();files=reconstruct(a.parent,a.patch)
    if a.output.exists():raise SystemExit('Output destination exists; refuse overwrite')
    a.output.mkdir(parents=True)
    for name,data in files.items():
        f=a.output/name
        f.parent.mkdir(parents=True,exist_ok=True)
        f.write_bytes(data)
    print(json.dumps({'status':'VERIFIED','files':len(files),'output':str(a.output)}))

if __name__=='__main__':main()
