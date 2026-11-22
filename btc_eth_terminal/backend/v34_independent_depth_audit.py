"""Independent full top-ten price/quantity check of v34 historical L2 projection.

Does NOT use v23.Book, v34.top_levels or the v34 source parser. It parses the
original pinned raw JSONL, applies Coinbase zero-delete L2 semantics using two
plain dictionaries and compares all twenty levels at each frozen decision.
Internal source agreement is not venue attestation or alpha evidence.
"""
from __future__ import annotations
import json
from decimal import Decimal
from hashlib import sha256
from datetime import datetime,timezone
from pathlib import Path
from v30_unified_projection import BASE_ROOT
from v34_l2_api import load_l2

class IndependentDepthReject(ValueError): pass

def strict_json(raw):
    def pairs(items):
        out={}
        for key,value in items:
            if key in out:raise IndependentDepthReject('duplicate JSON key')
            out[key]=value
        return out
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda s: (_ for _ in ()).throw(IndependentDepthReject('invalid numeric constant')))

def ms_utc(value):
    dt=datetime.fromisoformat(value.replace('Z','+00:00'))
    if dt.tzinfo is None or dt.utcoffset().total_seconds()!=0:raise IndependentDepthReject('non-UTC receive time')
    return int(dt.timestamp()*1000)

def replay(root:Path=BASE_ROOT):
    root=Path(root)
    if root.is_symlink():raise IndependentDepthReject('symlinked source')
    data,digest,base,base_hash=load_l2()
    manifest_bytes=(root/'manifest.json').read_bytes()
    if sha256(manifest_bytes).hexdigest()!=data['source_manifest_sha256']:
        raise IndependentDepthReject('original manifest digest mismatch')
    manifest=strict_json(manifest_bytes)
    records=data['products']
    grid=sorted({r['decision_receiver_ms'] for v in records.values() for r in v['depth_snapshots']})
    books={p:{'bid':{},'offer':{}} for p in records}
    initialized={p:False for p in records}
    snapshots={p:[] for p in records}
    prev_seq=None;prev_receive=None;grid_i=0;messages=0
    def emit(t):
        for p in sorted(records):
            row=next((r for r in records[p]['depth_snapshots'] if r['decision_receiver_ms']==t),None)
            if row is None:raise IndependentDepthReject('missing original decision')
            if row['depth'] is None:continue
            for side,desc in (('bid',True),('offer',False)):
                selected=sorted(books[p][side].items(),reverse=desc)[:10]
                projected=row['depth']['bids' if desc else 'asks']
                if len(selected)!=10 or len(projected)!=10:raise IndependentDepthReject('wrong depth count')
                running=Decimal(0)
                for (price,qty),r in zip(selected,projected):
                    running+=qty
                    if price!=Decimal(r['price']) or qty!=Decimal(r['quantity']) or running!=Decimal(r['cumulative_base']):
                        raise IndependentDepthReject('Top 10 exact Decimal price, size or cumulative discrepancy')
            snapshots[p].append(t)
    for chunk in manifest['chunks']:
        f=root/chunk['file']
        if f.is_symlink() or not f.is_file() or not f.resolve().is_relative_to(root.resolve()):
            raise IndependentDepthReject('invalid archived chunk path')
        raw=f.read_bytes()
        if sha256(raw).hexdigest()!=chunk['archive_sha256']:
            raise IndependentDepthReject('archived chunk SHA mismatch')
        for line in raw.splitlines():
            envelope=strict_json(line)
            payload=envelope['raw_text'].encode()
            if sha256(payload).hexdigest()!=envelope['sha256']:
                raise IndependentDepthReject('raw payload SHA mismatch')
            msg=strict_json(payload)
            seq=msg['sequence_num'];recv=ms_utc(envelope['received_utc']);messages+=1
            if prev_seq is not None and seq!=prev_seq+1:raise IndependentDepthReject('source sequence gap')
            if prev_receive is not None and recv<prev_receive:raise IndependentDepthReject('receive-time clock reversal')
            prev_seq=seq;prev_receive=recv
            while grid_i<len(grid) and grid[grid_i]<recv:
                emit(grid[grid_i]);grid_i+=1
            if msg['channel']!='l2_data':continue
            for evt in msg['events']:
                p=evt['product_id'];kind=evt['type']
                if p not in books:raise IndependentDepthReject('out-of-scope L2 product')
                if kind=='snapshot':
                    if initialized[p]:raise IndependentDepthReject('duplicate L2 snapshot')
                    initialized[p]=True
                elif kind!='update' or not initialized[p]:raise IndependentDepthReject('L2 update without snapshot')
                for u in evt['updates']:
                    side=u['side']
                    if side not in ('bid','offer'):raise IndependentDepthReject('bad side')
                    price=Decimal(u['price_level']);qty=Decimal(u['new_quantity'])
                    if not price.is_finite() or price<=0 or not qty.is_finite() or qty<0:
                        raise IndependentDepthReject('bad quantity/price')
                    if qty==0:books[p][side].pop(price,None)
                    else:books[p][side][price]=qty
    while grid_i<len(grid):emit(grid[grid_i]);grid_i+=1
    if messages!=data['source_messages']:raise IndependentDepthReject('raw message count mismatch')
    if (root/'manifest.json').read_bytes()!=manifest_bytes:raise IndependentDepthReject('source changed mid audit')
    return {'status':'INDEPENDENT_DEPTH_PARITY_PASS_RECEIVER_ONLY',
            'original_source_manifest_sha256':data['source_manifest_sha256'],
            'v34_depth_projection_sha256':digest,
            'original_messages_replayed':messages,
            'ten_level_observations':{p:len(rows) for p,rows in snapshots.items()},
            'price_quantity_cumulative_values_exact_match':sum(len(rows)*10*3*2 for rows in snapshots.values()),
            'oos_approved':False,'real_orders':0}

if __name__=='__main__':
    result=replay()
    path=Path(__file__).resolve().parents[1]/'V34_INDEPENDENT_DEPTH_REPLAY.json'
    path.write_text(json.dumps(result,sort_keys=True,indent=2)+'\n')
    print(json.dumps(result,sort_keys=True,indent=2))
