"""Independent verifier for v29 full and v28-pinned patch, read-only."""
from __future__ import annotations
import argparse,hashlib,json
from pathlib import Path
from zipfile import ZipFile
PARENT_SHA='bcb47312215c649bf3f828860897c14810a6bd37b022b98788bee003c74c1aeb'
MANIFEST='RELEASE_FILE_MANIFEST_v29.json'

def digest(blob):return hashlib.sha256(blob).hexdigest()
def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def audit_zip(path):
    with ZipFile(path) as z:
        names=[x.filename for x in z.infolist()]
        if len(names)!=len(set(names)) or any(n.startswith('/') or any(x in ('','.','..') for x in n.split('/')) for n in names):
            raise ValueError('Invalid/duplicate ZIP names')
        if z.testzip() is not None:raise ValueError('ZIP CRC mismatch')
        return {n:digest(z.read(n)) for n in names}

def verify(parent,full,patch):
    if sha(parent)!=PARENT_SHA:raise ValueError('Unrecognized v28 ZIP')
    old=audit_zip(parent);new=audit_zip(full);delta=audit_zip(patch)
    with ZipFile(full) as z:m=json.loads(z.read(MANIFEST))
    expected={x['path']:x['sha256'] for x in m['files']}
    if len(expected)!=len(m['files']) or new!={**expected,MANIFEST:new[MANIFEST]}:
        raise ValueError('Manifest hashes differ')
    if (m['release']!='v29' or m['parent_release']!='v28' or
            m['parent_zip_sha256']!=PARENT_SHA or
            m['latest_24h_capture_verified'] is not False or
            m['independent_blind_oos_approved'] is not False or
            m['confirmed_executable_alpha'] is not False or m['real_orders']!=0):
        raise ValueError('False research/24h claims in metadata')
    if {**old,**delta}!=new:raise ValueError('Patch fails to reproduce full ZIP')
    for name,d in old.items():
        if (name.startswith(('backend/data/','evidence/v27_github_real_60s/'))
             or name=='V28_SOURCE_COHORT_LEDGER.json') and new.get(name)!=d:
            raise ValueError('Original source/ledger altered: '+name)
    return {'schema':'v29_independent_release_verification_v1','status':'PASS',
            'parent_v28_sha256':sha(parent),'v29_full_sha256':sha(full),
            'v29_patch_sha256':sha(patch),'full_file_count':len(new),
            'manifest_data_files':len(expected),'patch_entries':len(delta),
            'source_preserved':True,'patch_reconstructs_entire_release':True,
            'original_data_file_count':sum(k.startswith('backend/data/') for k in old),
            'real_24h_verified':False,'confirmed_executable_alpha':False,
            'real_orders':0}

def main():
    p=argparse.ArgumentParser()
    for n in ('parent','full','patch','out'):p.add_argument('--'+n,type=Path,required=True)
    a=p.parse_args()
    if a.out.exists() or a.out.is_symlink():raise ValueError('Existing output')
    result=verify(a.parent,a.full,a.patch)
    a.out.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    print(json.dumps(result,indent=2))
if __name__=='__main__':main()
