"""Deterministic v26 standalone ZIP and v25-SHA-locked patch."""
from __future__ import annotations
import argparse
from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

PARENT = 'f5b2ba16dc57a4edb8f7ff1cb5595f0e7617141fee447a218a40419825ecd042'
MANIFEST = 'RELEASE_FILE_MANIFEST_v26.json'


def checksum(path):
    h=sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024), b''):
            h.update(b)
    return h.hexdigest()


def sources(root):
    root=Path(root).resolve()
    for file in sorted(root.rglob('*')):
        if (not file.is_file() or file.name==MANIFEST or
                file.suffix in ('.pyc','.pid') or '__pycache__' in file.parts):
            continue
        if file.is_symlink() or not file.resolve().is_relative_to(root):
            raise ValueError('Symlink/escape in source tree: '+str(file))
        yield file.relative_to(root).as_posix(), file


def zip_write(path, files):
    if path.exists() or path.is_symlink():
        raise ValueError('Refusing archive overwrite')
    with ZipFile(path,'w',compression=ZIP_DEFLATED,compresslevel=6,allowZip64=True) as z:
        for name,file in sorted(files):
            if name.startswith('/') or any(seg in ('','.','..') for seg in name.split('/')):
                raise ValueError('Unsafe archive member: '+name)
            entry=ZipInfo(name,date_time=(2026,11,15,0,0,0))
            entry.compress_type=ZIP_DEFLATED
            entry.external_attr=(0o644<<16)
            with file.open('rb') as src,z.open(entry,'w',force_zip64=True) as dst:
                for block in iter(lambda:src.read(1024*1024),b''):
                    dst.write(block)
    with ZipFile(path) as z:
        if z.testzip() is not None:
            raise ValueError('CRC verification failed')


def create(root,parent,full,patch):
    if checksum(parent)!=PARENT:
        raise ValueError('Wrong v25 parent package')
    with ZipFile(parent) as old:
        prior={name: sha256(old.read(name)).hexdigest() for name in old.namelist()
               if not name.endswith('/')}
    all_source=list(sources(root))
    desc={'release':'v26','parent_release':'v25',
          'parent_zip_sha256':PARENT,
          'scope':'PERIODIC_HOST_EVIDENCE_HARDENING_NOT_INDEPENDENT_TRUTH',
          'real_60s_public_capture_in_v26_environment':False,
          'actual_24h_verified':False,'original_research_feature_label_pit_verified':False,
          'independent_oos_alpha_verified':False,'broker_access_authorized':False,
          'real_orders_sent':0,
          'files':[{'path':name,'size_bytes':file.stat().st_size,'sha256':checksum(file)}
                   for name,file in all_source]}
    manifest_path=Path(root)/MANIFEST
    manifest_path.write_text(json.dumps(desc,sort_keys=True,indent=2)+'\n')
    all_files=all_source+[(MANIFEST,manifest_path)]
    changed=[(name,file) for name,file in all_files if prior.get(name)!=checksum(file)]
    zip_write(full,all_files)
    zip_write(patch,changed)
    return {'release':'v26','parent_zip_sha256':PARENT,
            'full_zip_sha256':checksum(full), 'full_zip_bytes':full.stat().st_size,
            'full_zip_entries':len(all_files),'manifest_files':len(all_source),
            'patch_zip_sha256':checksum(patch),'patch_zip_bytes':patch.stat().st_size,
            'patch_entries':len(changed),'changed_paths':[x[0] for x in changed],
            'historical_source_unchanged':True,
            'real_24h_verified':False,'executable_alpha_verified':False,
            'real_orders_sent':0}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    for key in ('root','parent','full','patch','report'):
        ap.add_argument('--'+key,type=Path,required=True)
    args=ap.parse_args()
    result=create(args.root,args.parent,args.full,args.patch)
    if args.report.exists():
        raise SystemExit('Report file already exists')
    args.report.write_text(json.dumps(result,sort_keys=True,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='changed_paths'},indent=2))


if __name__=='__main__':main()
