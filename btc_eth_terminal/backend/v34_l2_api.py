"""Immutable, bounded, receiver-asof L2 and post-hoc recovery API.

The UI must never accept a different source cohort or a future-completed
recovery observation into an earlier historical decision.
"""
from __future__ import annotations
from hashlib import sha256
from pathlib import Path
import json
from decimal import Decimal, InvalidOperation
from v30_unified_api import load_projection as load_base, UnifiedAPIError, _prod, _number
from v30_unified_projection import _strict_json, PRODS
from v34_l2_depth import OUT, SCHEMA, DEPTH

# Immutable release-specific pin. A rewritten projection+sidecar cannot be promoted.
PINNED_L2_SHA256='9c93c9a53ea4c03937198886ede3df71c03af03691349c245d0d8506246b88de'
class L2APIError(ValueError): pass


def _validate_recovery_events(product: str, events: object, window_ms: int) -> None:
    """Defensive invariants independent of pinning the serialized artifact.

    The source PIN is still required. These checks prevent a new, legitimately
    rebuilt release from elevating internally inconsistent recovery outcomes.
    They do not convert the retrospective study into an alpha prediction.
    """
    if not isinstance(events, list) or len(events) > 100_000:
        raise L2APIError('Invalid bounded recovery event inventory')
    previous_shock = -1
    for event in events:
        if not isinstance(event, dict) or event.get('product') != product:
            raise L2APIError('Cross-product or malformed recovery event')
        shock = event.get('shock_receiver_ms')
        available = event.get('available_at_ms')
        delay = event.get('first_recovery_delay_ms')
        recovered = event.get('recovery_within_30s')
        censored = event.get('censored_at_30s')
        if (type(shock) is not int or type(available) is not int
            or shock <= previous_shock or available != shock + window_ms
            or type(recovered) is not bool or type(censored) is not bool
            or recovered == censored):
            raise L2APIError('Recovery event time or censoring contract invalid')
        previous_shock = shock
        try:
            baseline = Decimal(event['baseline_30s_median_depth_base'])
            shock_depth = Decimal(event['shock_depth_base'])
            threshold = Decimal(event['recovery_threshold_base'])
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise L2APIError('Recovery depth is malformed') from exc
        if (not all(v.is_finite() and v > 0 for v in (baseline, shock_depth, threshold))
            or shock_depth > baseline * Decimal('0.5')
            or threshold != baseline * Decimal('0.8')):
            raise L2APIError('Recovery threshold or shock depth is contradictory')
        if recovered:
            if (type(delay) is not int or not 1_000 <= delay <= window_ms
                or delay % 1_000 != 0):
                raise L2APIError('Recovery delay cannot be inferred from this window')
        elif delay is not None:
            raise L2APIError('Censored recovery event cannot have a recovery delay')

def _bounded(path:Path,max_bytes:int)->bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size>max_bytes:
        raise L2APIError('Untrusted or absent immutable L2 evidence')
    return path.read_bytes()

def load_l2():
    raw=_bounded(OUT/'projection.json',2_000_000)
    pin=_strict_json(_bounded(OUT/'projection.sha256.json',8_000))
    digest=sha256(raw).hexdigest()
    base,base_hash=load_base()
    data=_strict_json(raw)
    if (digest!=PINNED_L2_SHA256
        or pin.get('schema')!='v34_l2_depth_pin_v1'
        or pin.get('projection_sha256')!=digest
        or pin.get('base_projection_sha256')!=base_hash
        or pin.get('source_manifest_sha256')!=base['source_manifest_sha256']
        or data.get('schema')!=SCHEMA
        or data.get('source_manifest_sha256')!=base['source_manifest_sha256']
        or data.get('base_projection_sha256')!=base_hash
        or data.get('source_messages')!=base['source_message_count']
        or data.get('live_feed') is not False
        or data.get('oos_approved') is not False
        or data.get('trading_enabled') is not False
        or data.get('execution_authorized') is not False
        or data.get('historical_only') is not True
        or set(data.get('products',{}))!=set(PRODS)):
        raise L2APIError('L2 projection identity/lineage/safety mismatch')
    if (type(data.get('recovery_window_ms')) is not int or
        data['recovery_window_ms'] != 30_000):
        raise L2APIError('Unapproved recovery outcome horizon')
    for p in PRODS:
        source=base['products'][p]['features']
        rows=data['products'][p]['depth_snapshots']
        if len(rows)!=len(source):raise L2APIError('Source decision grid mismatch')
        for a,b in zip(rows,source):
            if (a['product']!=p or a['decision_receiver_ms']!=b['decision_receive_utc_ms']
                or a['source_sequence']!=b['source_message_sequence']
                or a['source_age_ms']!=b['source_age_ms']):
                raise L2APIError('L2 decision breaks original receiver-asof reference')
            levels=a.get('depth')
            if b['status']=='DIAGNOSTIC_CAUSAL_L1':
                if a.get('status')!='HISTORICAL_L2_TEN_LEVEL' or not isinstance(levels,dict):
                    raise L2APIError('Previously admissible L1 quote missing its L2')
                try:
                    for side,price_key,qty_key,descending in (
                      ('bids','best_bid','best_bid_qty',True),('asks','best_ask','best_ask_qty',False)):
                        arr=levels[side]
                        if len(arr)!=DEPTH:raise L2APIError('Depth must contain ten levels per side')
                        prices=[Decimal(v['price']) for v in arr]
                        qty=[Decimal(v['quantity']) for v in arr]
                        cumulative=[Decimal(v['cumulative_base']) for v in arr]
                        if (not all(x.is_finite() and x>0 for x in prices+qty+cumulative)
                            or any((prices[i]<=prices[i+1] if descending else prices[i]>=prices[i+1]) for i in range(DEPTH-1))
                            or [sum(qty[:i+1],Decimal(0)) for i in range(DEPTH)]!=cumulative
                            or Decimal(levels['bid_total_base_10' if descending else 'ask_total_base_10']) != cumulative[-1]
                            or prices[0]!=Decimal(b[price_key]) or qty[0]!=Decimal(b[qty_key])):
                            raise L2APIError('Invalid L2 ladder or mismatch with frozen L1')
                    if prices[0]<=Decimal(levels['bids'][0]['price']):
                        raise L2APIError('Crossed or locked historical L2 book')
                except (KeyError,TypeError,InvalidOperation,IndexError) as exc:
                    raise L2APIError('Malformed or contradictory L2 book') from exc
            elif levels is not None or a.get('status')!='NO_ADMISSIBLE_L2':
                raise L2APIError('L2 quote leaked through invalid L1 decision')
        _validate_recovery_events(p, data['products'][p].get('recovery_events'), data['recovery_window_ms'])
    return data,digest,base,base_hash

def _at_decision(data,product,asof_ms):
    p=_prod(product); t=_number(asof_ms,'asof')
    matches=[r for r in data['products'][p]['depth_snapshots'] if r['decision_receiver_ms']==t]
    if len(matches)!=1:raise L2APIError('L2 evidence available only at frozen diagnostic decisions')
    return matches[0],t

def depth(product:str,asof_ms:int):
    data,digest,base,base_hash=load_l2()
    item,t=_at_decision(data,product,asof_ms)
    return {'product':product,'asof_receiver_ms':t,'row':item,
            'levels_each_side':DEPTH,'source_sha256':data['source_manifest_sha256'],
            'base_projection_sha256':base_hash,'l2_projection_sha256':digest,
            'receiver_time_only':True,'historical_only':True,
            'live_feed':False,'oos_approved':False,'trading_enabled':False,
            'definition':data['ten_level_definition']}

def recovery(product:str,asof_ms:int,limit:int=8):
    data,digest,base,base_hash=load_l2()
    _,t=_at_decision(data,product,asof_ms)
    if type(limit) is not int or not 1<=limit<=30:raise L2APIError('Invalid bounded recovery event limit')
    all_events=data['products'][product]['recovery_events']
    visible=[e for e in all_events if e['available_at_ms']<=t]
    return {'product':product,'asof_receiver_ms':t,'events':visible[-limit:],
            'visible_events_count':len(visible),'window_ms':data['recovery_window_ms'],
            'source_sha256':data['source_manifest_sha256'],
            'base_projection_sha256':base_hash,'l2_projection_sha256':digest,
            'receiver_time_only':True,'historical_only':True,
            'live_feed':False,'oos_approved':False,'trading_enabled':False,
            'definition':data['recovery_definition'],
            'eligibility':'POST_HOC_DESCRIPTIVE_ONLY_NO_ALPHA'}