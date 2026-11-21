"""Byte-for-byte retained market-source and historical-lineage release inventory."""
from hashlib import sha256
from pathlib import Path
import json
import zipfile
import argparse

ORIGINAL_MANIFEST_SHA='08255d79f0a7f081921f370feca2e92686cde3dffbb406eadc23e918d43e73d7'


def files_from_zip(path):
    with zipfile.ZipFile(path) as z:
        if z.testzip() is not None:
            raise ValueError('ZIP CRC failure: '+str(path))
        data={n:sha256(z.read(n)).hexdigest() for n in z.namelist() if n.startswith('backend/data/') and not n.endswith('/')}
        return data


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--release-root',type=Path,required=True)
    ap.add_argument('--v18',type=Path,required=True)
    ap.add_argument('--v19-a',type=Path,required=True)
    ap.add_argument('--v19-b',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    args=ap.parse_args()
    root=args.release_root
    local={p.relative_to(root).as_posix():sha256(p.read_bytes()).hexdigest() for p in (root/'backend/data').rglob('*') if p.is_file()}
    originals={name:files_from_zip(path) for name,path in [('v18',args.v18),('v19_a',args.v19_a),('v19_b',args.v19_b)]}
    if len(local)!=206 or any(local != hashes for hashes in originals.values()):
        raise ValueError('Historical original 206-file byte identity failed')
    mf=root/'backend/data/live_v16_one_hour/coinbase_btc_eth_soak_eE9SNH/manifest.json'
    if sha256(mf.read_bytes()).hexdigest()!=ORIGINAL_MANIFEST_SHA:
        raise ValueError('Canonical original one-hour Coinbase manifest SHA changed')
    a_report=(root/'P1_MAKER_QUEUE_SCENARIOS_v19.json').read_bytes()
    b_report=(root/'reference/v19_b/P1_MAKER_TAPE_ONE_HOUR_v19.json').read_bytes()
    with zipfile.ZipFile(args.v19_a) as z:
        if z.read('P1_MAKER_QUEUE_SCENARIOS_v19.json')!=a_report:
            raise ValueError('Original A report mutated')
    with zipfile.ZipFile(args.v19_b) as z:
        if z.read('P1_MAKER_TAPE_ONE_HOUR_v19.json')!=b_report:
            raise ValueError('Original B report mutated')
    return_value={
      'status':'PRESERVED_V18_V19A_V19B_AND_V20_SOURCE_BYTES_IDENTICAL',
      'source_file_count':len(local),
      'source_sha256_census_same_for_all_releases':True,
      'original_one_hour_manifest_sha256':ORIGINAL_MANIFEST_SHA,
      'parent_v18_sha256':sha256(args.v18.read_bytes()).hexdigest(),
      'parent_v19_a_sha256':sha256(args.v19_a.read_bytes()).hexdigest(),
      'parent_v19_b_sha256':sha256(args.v19_b.read_bytes()).hexdigest(),
      'original_maker_report_a_unchanged':True,'original_maker_report_b_unchanged':True,
      'independent_market_data_source_verified':False,
      'actual_24h_collection_verified':False,'original_features_labels_available':False,
    }
    args.output.write_text(json.dumps(return_value,indent=2,sort_keys=True)+'\n')
    print(json.dumps(return_value,indent=2))

if __name__=='__main__':main()
