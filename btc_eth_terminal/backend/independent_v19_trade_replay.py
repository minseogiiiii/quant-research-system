"""Independent v19 counter of exact-price public trade-flow observations.

Recomputes quantities from original bytes without importing v19's maker engine,
state machine, quote engine or book model. It verifies observational aggregates,
NOT a queue position, fill, or independently sourced exchange truth.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sys


def _load(raw):
    def pairs(v):
        x = {}
        for k, y in v:
            if k in x:
                raise ValueError('Duplicate JSON member')
            x[k] = y
        return x
    def bad(v):
        raise ValueError('Nonfinite JSON token')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=bad)


def _time(value):
    if not isinstance(value,str) or not value.endswith('Z'):
        raise ValueError('Invalid UTC string')
    obj = datetime.fromisoformat(value[:-1] + '+00:00')
    if obj.utcoffset() != timezone.utc.utcoffset(obj):
        raise ValueError('Invalid UTC timezone')
    return obj


def verify(root: Path, report: dict) -> dict:
    root = Path(root)
    if root.is_symlink():
        raise ValueError('Source must not be symlink')
    manifest_path = root/'manifest.json'
    if manifest_path.is_symlink():
        raise ValueError('Manifest must not be symlink')
    manifest_bytes = manifest_path.read_bytes()
    if sha256(manifest_bytes).hexdigest() != report['source_manifest_sha256']:
        raise ValueError('Archive manifest does not match candidate report')
    manifest = _load(manifest_bytes)
    window = report['resting_window_ms_predeclared']*1_000_000
    outputs = [x for x in report['results'] if x['status'] in
               ('CONDITIONAL_FLOW_DIAGNOSTIC_COMPLETE','INCOMPLETE_RESTING_WINDOW')]
    if len({x['id'] for x in outputs}) != len(outputs):
        raise ValueError('Duplicate candidate')
    by_anchor = {}
    for c in outputs:
        by_anchor.setdefault(c['anchor_sequence'],[]).append(c)
    anchors = {}
    accum = {c['id']: {'volume':Decimal(0),'n':0,'side_volume':Decimal(0),
                      'dropped':0,'first':None} for c in outputs}
    known_ids = {}
    num_messages = 0
    last_clock = -1
    for chunk in manifest['chunks']:
        archive = root/chunk['file']
        if archive.is_symlink() or not archive.is_file() or not archive.resolve().is_relative_to(root.resolve()):
            raise ValueError('Unsafe archive')
        blob = archive.read_bytes()
        if sha256(blob).hexdigest()!=chunk['archive_sha256'] or len(blob)!=chunk['archived_line_bytes']:
            raise ValueError('Archive bytes changed')
        for line in blob.splitlines(keepends=True):
            if not line.endswith(b'\n'):
                raise ValueError('Truncated archive row')
            raw = _load(line)
            raw_message = raw['raw_text']
            if sha256(raw_message.encode()).hexdigest()!=raw['sha256']:
                raise ValueError('Corrupt source message')
            message = _load(raw_message)
            if type(message['sequence_num']) is not int or message['sequence_num']!=num_messages:
                raise ValueError('Sequence gap')
            stamp = raw['local_monotonic_ns']
            if not isinstance(stamp,str) or not stamp.isdecimal():
                raise ValueError('Bad monotonic stamp')
            ns = int(stamp)
            if ns<last_clock:
                raise ValueError('Monotonic receipt clock regression')
            last_clock = ns
            for c in by_anchor.get(num_messages,[]):
                if raw['received_utc']!=c['anchor_received_utc']:
                    raise ValueError('Quote anchor wall time mismatch')
                anchors[c['id']] = (ns,_time(raw['received_utc']))
            if message['channel']=='market_trades':
                for ev in message['events']:
                    if ev['type']!='update':
                        continue
                    for t in ev['trades']:
                        k=(t['product_id'],t['trade_id'])
                        identity=(t['price'],t['size'],t['time'],t['side'])
                        if k in known_ids:
                            if known_ids[k]!=identity:
                                raise ValueError('Conflicting duplicate trade ID')
                            continue
                        known_ids[k]=identity
                        price=Decimal(t['price'])
                        qty=Decimal(t['size'])
                        when=_time(t['time'])
                        for c in outputs:
                            anchor=anchors.get(c['id'])
                            if (anchor is None or t['product_id']!=c['product_id'] or
                                price!=Decimal(c['posted_price_hypothetical']) or
                                not anchor[0]<ns<=anchor[0]+window):
                                continue
                            s=accum[c['id']]
                            if when<anchor[1]:
                                s['dropped']+=1
                            else:
                                s['volume']+=qty
                                s['n']+=1
                                if t['side']==c['side']:
                                    s['side_volume']+=qty
                                if s['first'] is None:
                                    s['first']=num_messages
            num_messages+=1
    if num_messages!=report['source_messages_audited'] or manifest_path.read_bytes()!=manifest_bytes:
        raise ValueError('Source source count/manifest mutation')
    if set(anchors)!={c['id'] for c in outputs}:
        raise ValueError('Incomplete candidate anchor list')
    verified=[]
    for c in outputs:
        s=accum[c['id']]
        expectations={
            'eligible_trade_count_any_side_exact_price':s['n'],
            'eligible_trade_volume_any_side_exact_price':str(s['volume']),
            'side_matching_maker_label_volume_diagnostic_only':str(s['side_volume']),
            'pre_anchor_exchange_time_trade_exclusions':s['dropped'],
            'first_qualifying_trade_sequence':s['first']}
        for field,expected in expectations.items():
            actual=c[field]
            if field in ('eligible_trade_volume_any_side_exact_price', 'side_matching_maker_label_volume_diagnostic_only'):
                if Decimal(actual)!=Decimal(expected):
                    raise ValueError(c['id']+' mismatched '+field)
            elif actual!=expected:
                raise ValueError(c['id']+' mismatched '+field)
        verified.append(c['id'])
    return {'status':'INDEPENDENT_SOFTWARE_OBSERVATION_PARITY_NOT_ORDER_FILL_TRUTH',
            'source_messages_verified':num_messages,'candidates_verified':verified,
            'source_manifest_sha256':report['source_manifest_sha256'],
            'independent_market_source_used':False,'real_order_fills_verified':False}


def main():
    if len(sys.argv)!=4:
        raise SystemExit('usage: python independent_v19_trade_replay.py ARCHIVE REPORT OUT')
    result=verify(Path(sys.argv[1]), _load(Path(sys.argv[2]).read_bytes()))
    Path(sys.argv[3]).write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result))


if __name__=='__main__':
    main()
