"""Independent v28 full/patch release verifier with exact manifest file SHA checks."""
from __future__ import annotations
import argparse,hashlib,json
from pathlib import Path
from zipfile import ZipFile
MANIFEST='RELEASE_FILE_MANIFEST_v28.json'

def digest(blob):return hashlib.sha256(blob).hexdigest()
def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def audited_zip(path):
    with ZipFile(path) as z:
        items=z.infolist()
        names=[x.filename for x in items]
        if len(names)!=len(set(names)) or any(n.startswith('/') or any(p in ('','.','..') for p in n.split('/')) for n in names):
            raise ValueError('Unsafe ZIP members')
        if z.testzip() is not None:raise ValueError('ZIP CRC failure')
        return {n:digest(z.read(n)) for n in names}

def verify(parent,full,patch):
    if sha(parent)!='b70b612c0a1d980c95b7af33d9e28b5cac1056e60dcf4d7c534fdb411c36a8f0':
        raise ValueError('Wrong v27 ZIP')
    old=audited_zip(parent)
    final=audited_zip(full)
    delta=audited_zip(patch)
    with ZipFile(full) as z:
        m=json.loads(z.read(MANIFEST))
    expected={x['path']:x['sha256'] for x in m['files']}
    if len(expected)!=len(m['files']) or final!={**expected,MANIFEST:final[MANIFEST]}:
        raise ValueError('v28 file manifest mismatch')
    if (m['release']!='v28' or m['cohort_count']!=11 or
            m['blind_oos_alpha_verified'] is not False or m['real_orders']!=0):
        raise ValueError('Unexpected research claim')
    recomposed={**old,**delta}
    if recomposed!=final:raise ValueError('Patch not equivalent to full ZIP')
    for name,checksum in old.items():
        if name.startswith(('backend/data/','evidence/v27_github_real_60s/')) and final.get(name)!=checksum:
            raise ValueError('Mutated original research evidence')
    return {'schema':'v28_independent_package_verification_v1','status':'PASS',
            'v27_parent_sha256':sha(parent),'v28_full_sha256':sha(full),
            'v28_patch_sha256':sha(patch),'v28_full_file_count':len(final),
            'v28_manifest_data_files':len(expected),'v28_patch_entries':len(delta),
            'full_manifest_sha256_verified':True,
            'delta_recomposes_exact_v28_file_bytes':True,
            'preserved_original_source_files':sum(n.startswith('backend/data/') for n in old),
            'preserved_v27_raw_artifact':True,'originals_modified':False,
            'live_orders':0,'confirmed_alpha':False}

def main():
    a=argparse.ArgumentParser()
    for k in ('parent','full','patch','out'):a.add_argument('--'+k,type=Path,required=True)
    args=a.parse_args()
    if args.out.exists():raise ValueError('Report exists')
    result=verify(args.parent,args.full,args.patch)
    args.out.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    print(json.dumps(result,indent=2))
if __name__=='__main__':main()
