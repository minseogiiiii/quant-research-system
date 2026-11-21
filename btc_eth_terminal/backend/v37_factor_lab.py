"""Read-only Q7 Factor Lab built on the frozen v23 OFI / v34 depth engines.

Only historical developmental diagnostics are produced. Not a blind OOS test,
execution backtest, account feed, maker fill or live order interface.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
from statistics import median

from v23_causal_microstructure import features_from_events, read_l2, WINDOW_MS
from v30_unified_api import load_projection
from v30_unified_projection import BASE_ROOT, HERE, _strict_json, PRODS
from v34_l2_api import load_l2
from v21_source_evidence import iso_ms

SCHEMA = 'q7_factor_lab_v1'
DEST = HERE / 'data' / 'q7_factor_lab' / 'report.json'
PIN = DEST.with_name('report.sha256.json')
RESEARCH_ID = 'Q7-OFI-LIQUIDITY-RECOVERY-001'
PREREG = HERE/'q7_experiment_prereg.json'
PREREG_PIN = HERE/'q7_experiment_prereg.sha256.json'

def preregistration_digest():
    if PREREG.is_symlink() or PREREG_PIN.is_symlink():
        raise FactorLabReject('Symlinked research registration')
    original=PREREG.read_bytes()
    expected=_strict_json(PREREG_PIN.read_bytes())
    digest=sha256(original).hexdigest()
    protocol=_strict_json(original)
    if (digest!=expected.get('preregistration_sha256') or
        protocol.get('experiment_id')!=RESEARCH_ID or
        protocol.get('registered_before_q7_label_extraction') is not True or
        protocol.get('trial_budget')!=8 or
        protocol.get('development_archive_role')!='ALREADY_EXPLORED_FIXTURE_NOT_OOS' or
        protocol.get('split',{}).get('blind_final') is not None or
        protocol.get('orders_enabled') is not False):
        raise FactorLabReject('Unverified preregistration identity')
    return digest

GRID_MS = 5_000
PRIMARY_HORIZON_MS = 60_000
MIN_INDEPENDENT_DAYS = 10  # gate, NOT a claim of statistical sufficiency
MAX_SOURCE_CHUNKS = 200


class FactorLabReject(ValueError): pass


def dec(v, *, positive=False, allow_zero=False):
    if not isinstance(v, (str, int)) or isinstance(v, bool):
        raise FactorLabReject('Expected decimal string or integer')
    try: d = Decimal(str(v))
    except InvalidOperation as exc: raise FactorLabReject('Invalid decimal') from exc
    if not d.is_finite() or (positive and d <= 0) or (not positive and d < 0):
        raise FactorLabReject('Nonfinite or out-of-domain decimal')
    return d


def reference_mid(row):
    if row.get('status') != 'DIAGNOSTIC_CAUSAL_L1' or row.get('source_age_ms') is None or row['source_age_ms'] > 5000:
        return None
    bid = dec(row['best_bid'],positive=True); ask=dec(row['best_ask'],positive=True)
    if bid>=ask: raise FactorLabReject('Invalid book')
    return (bid+ask)/2


def receiver_grid(manifest):
    start = iso_ms(manifest['sampled_from_utc'])
    stop = iso_ms(manifest['finished_at_utc'])
    first = (start + GRID_MS - 1)//GRID_MS*GRID_MS
    return list(range(first,stop+1,GRID_MS))


def feature_rows_to_diagnostics(rows):
    """Compute only at minute decision epochs, never using future labels as features."""
    byproduct = defaultdict(dict)
    for row in rows:
        if row['instrument'] not in PRODS or type(row['decision_receive_utc_ms']) is not int:
            raise FactorLabReject('Invalid product/time')
        byproduct[row['instrument']][row['decision_receive_utc_ms']]=row
    out={}
    for p in PRODS:
        lookup=byproduct[p]
        tpoints=sorted(lookup)
        if any(tpoints[i]-tpoints[i-1]!=GRID_MS for i in range(1,len(tpoints))):
            raise FactorLabReject('Noncontiguous receiver grid')
        signals=[]
        for t in tpoints:
            if t % 60_000 != 0:continue
            now=lookup[t];mid=reference_mid(now)
            if mid is None:continue
            prev=lookup.get(t-60_000)
            prevmid=reference_mid(prev) if prev else None
            fields={
                'ofi_l1_rolling_5m_base':now['ofi_rolling_5m_base'],
                'obi_l1':now['best_imbalance'],
                'microprice_displacement_bps':str((dec(now['microprice'],positive=True)-mid)/mid*10_000),
                'spread_bps':str((dec(now['best_ask'],positive=True)-dec(now['best_bid'],positive=True))/mid*10_000),
                'momentum_previous_60s_bps':str((mid-prevmid)/prevmid*10_000) if prevmid is not None else None,
                'l2_updates_rolling_5m':now['l2_update_events_rolling_5m'],
            }
            # Lookups below are ONLY used for outcomes in a separate structure.
            labels={}
            for horizon in (5_000,30_000,60_000):
                future=lookup.get(t+horizon)
                fmid=reference_mid(future) if future else None
                labels[str(horizon)]={
                    'available_after_receiver_ms':t+horizon,
                    'status':'HISTORICAL_FUTURE_MIDPRICE_DIAGNOSTIC_ONLY' if fmid is not None else 'CENSORED',
                    'forward_mid_return_bps':str((fmid-mid)/mid*10_000) if fmid is not None else None,
                }
            signals.append({'product':p,'decision_receiver_ms':t,'feature_max_received_ms':now['feature_max_received_utc_ms'],
                            'source_message_sequence_local_connection':now['source_message_sequence'],
                            'source_age_ms':now['source_age_ms'],'features_asof':fields,
                            'future_only_diagnostic_labels':labels})
        out[p]=signals
    return out


def static_walk(depth, side, size, fee_bps, extra_slippage_bps, displayed_depth_fraction='1'):
    """Frozen displayed 10-level taker reference. NO fills, no market impact.

    Explicit fee and non-book incremental slippage are distinct from book walk.
    Spread is already embedded in VWAP and must never be added again.
    """
    if side not in ('BUY','SELL'):raise FactorLabReject('Unknown side')
    quantity=dec(size, positive=True)
    fee=dec(fee_bps,allow_zero=True);slip=dec(extra_slippage_bps,allow_zero=True)
    fraction=dec(displayed_depth_fraction,positive=True)
    if fraction>1:raise FactorLabReject('Depth fraction cannot exceed displayed depth')
    if fee>100 or slip>200:raise FactorLabReject('Unbounded fee scenario')
    levels=depth['asks'] if side=='BUY' else depth['bids']
    if not 1<=len(levels)<=10:raise FactorLabReject('Unbounded book')
    remaining=quantity;used=[];notional=Decimal(0)
    last=None
    for record in levels:
        price=dec(record['price'],positive=True);available=dec(record['quantity'],positive=True)*fraction
        if last is not None and ((side=='BUY' and price<=last) or (side=='SELL' and price>=last)):
            raise FactorLabReject('Unsorted or duplicated ladder')
        last=price
        take=min(available,remaining)
        if take>0:
            used.append({'price':str(price),'quantity':str(take)})
            notional+=price*take
            remaining-=take
    best_bid=dec(depth['bids'][0]['price'],positive=True)
    best_ask=dec(depth['asks'][0]['price'],positive=True)
    if best_bid>=best_ask:raise FactorLabReject('Crossed ladder')
    mid=(best_bid+best_ask)/2
    vwap=notional/quantity if remaining==0 else None
    # Book-crossing and spread are in VWAP; add only explicit external assumptions.
    book_crossing_bps=((vwap-mid)/mid if side=='BUY' else (mid-vwap)/mid)*10_000 if vwap is not None else None
    total_cost_bps=(book_crossing_bps+fee+slip) if book_crossing_bps is not None else None
    status='STATIC_L2_FULL_QUANTITY_REFERENCE_NOT_FILL' if remaining==0 else 'CENSORED_INSUFFICIENT_DISPLAYED_DEPTH'
    return {'status':status,'requested_base':str(quantity),'covered_base':str(quantity-remaining),
            'uncovered_base':str(remaining),'side':side,'used_levels':used,
            'reference_vwap':str(vwap) if vwap is not None else None,
            'book_crossing_vs_mid_bps':str(book_crossing_bps) if book_crossing_bps is not None else None,
            'total_indicative_taker_cost_bps_assumed':str(total_cost_bps) if total_cost_bps is not None else None,
            'displayed_depth_fraction_assumed':str(fraction),
            'fee_bps_assumed':str(fee),'extra_non_book_slippage_bps_assumed':str(slip),
            'spread_double_counted':False,'real_fills':False,
            'impact_and_queue_model':'NOT_MODELED'}


def summarize(rows):
    """Descriptive count only. No fitted coefficients/p-values/OOS evidence."""
    outputs={}
    for p,signals in rows.items():
        outputs[p]={
            'feature_rows':len(signals),
            'primary_60s_future_mid_labels':sum(s['future_only_diagnostic_labels']['60000']['status'].startswith('HISTORICAL_') for s in signals),
            'auxiliary_5s_future_mid_labels':sum(s['future_only_diagnostic_labels']['5000']['status'].startswith('HISTORICAL_') for s in signals),
            'auxiliary_30s_future_mid_labels':sum(s['future_only_diagnostic_labels']['30000']['status'].startswith('HISTORICAL_') for s in signals),
            'primary_horizon_ms':PRIMARY_HORIZON_MS,
            'oos_status':'INSUFFICIENT_EVIDENCE_ONE_PREVIOUSLY_EXPLORED_HOUR',
            'out_of_sample_predictive_effect':None,
            'cost_adjusted_return_bps':None,
            'confidence_interval':None,
        }
    return outputs


def build(source=BASE_ROOT):
    prereg_sha=preregistration_digest()
    source=Path(source)
    if source.is_symlink() or not source.is_dir():raise FactorLabReject('No safe archived source root')
    root=source.resolve(strict=True)
    manifest_file=root/'manifest.json'
    if manifest_file.is_symlink():raise FactorLabReject('Unsafe source manifest')
    mbytes=manifest_file.read_bytes();m=_strict_json(mbytes)
    manifest_sha=sha256(mbytes).hexdigest()
    if (not isinstance(m.get('chunks'),list) or len(m['chunks'])>MAX_SOURCE_CHUNKS
        or m.get('products')!=list(PRODS) or m.get('status')!='CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH'):
        raise FactorLabReject('Unadmitted archived manifest')
    if any(c.get('connection_id')!=0 for c in m['chunks']):raise FactorLabReject('Multiple connection IDs not admitted')
    historical,pin=load_projection()
    if historical['source_manifest_sha256']!=manifest_sha:raise FactorLabReject('Frozen historical source changed')
    l2,l2_sha,base,base_sha=load_l2()
    if l2['source_manifest_sha256']!=manifest_sha or l2['base_projection_sha256']!=pin:
        raise FactorLabReject('Depth and feature lineage mismatch')
    grid=receiver_grid(m)
    if len(grid)>15_000:raise FactorLabReject('Unbounded grid')
    features=features_from_events(read_l2(root,m['chunks']),grid)
    signals=feature_rows_to_diagnostics(features)
    depths={}
    scenario_sizes={'BTC-USD':('0.005','0.01','0.05'),'ETH-USD':('0.1','0.5','1')}
    for p in PRODS:
        depth_samples=[]
        for x in l2['products'][p]['depth_snapshots']:
            if x['status']!='HISTORICAL_L2_TEN_LEVEL' or x['depth'] is None:continue
            scenarios=[]
            for size in scenario_sizes[p]:
                for side in ('BUY','SELL'):
                    for fee,slip,frac,label in [('0','0','1','ZERO_FEE_BOOK_ONLY'),('10','5','1','ASSUMED_BASE'),('20','10','0.5','ASSUMED_STRESS_HALF_DEPTH')]:
                        scenarios.append({'scenario':label,**static_walk(x['depth'],side,size,fee,slip,frac)})
            depth_samples.append({'decision_receiver_ms':x['decision_receiver_ms'],'source_sequence':x['source_sequence'],
                                  'product':p,'scenarios':scenarios})
        depths[p]=depth_samples
    if manifest_file.read_bytes()!=mbytes:raise FactorLabReject('Archive changed during research')
    # Pinned unchanged source chunks have been rehashed by the v23 source reader.
    result={
      'schema':SCHEMA,'experiment_id':RESEARCH_ID,'preregistration_sha256':prereg_sha,'status':'DEVELOPMENT_ARCHIVE_DESCRIPTIVE_ONLY',
      'source_type':'VERIFIED_HISTORICAL_ARCHIVE_NOT_LIVE','session_scope':'ONE_CAPTURE_ONE_CONNECTION',
      'source_manifest_sha256':manifest_sha,'source_message_count':historical['source_message_count'],
      'v30_projection_sha256':pin,'v34_depth_projection_sha256':l2_sha,
      'source_start_receiver_ms':historical['source_first_received_at_ms'],
      'source_end_receiver_ms':historical['source_finished_at_ms'],
      'source_clock':'UTC_LOCAL_RECEIVER_NOT_EXCHANGE_ATTESTED',
      'ofi_engine':'v23_causal_microstructure.features_from_events + ofi_delta',
      'ofi_definition':'L1 bid/offer best-price-or-size delta on each Coinbase L2 event batch; snapshot excluded; five-minute rolling receiver-time window; NOT atomic exchange order flow',
      'trade_imbalance_status':'NOT_CALCULATED_ARCHIVE_TRADES_SUBSAMPLED_IN_V30',
      'liquidity_recovery_status':'V34_POST_HOC_COHORT_ONLY_DO_NOT_SELECT_AT_SHOCK_TIME_WITH_FUTURE_OUTCOME',
      'label_horizons_ms':[60_000,5_000,30_000],
      'signals_with_future_labels_isolated':signals,'sample_summary':summarize(signals),
      'static_depth_cost_references':depths,
      'execution_status':'HISTORICAL_BOOK_WALK_REFERENCE_ONLY_NO_FILLED_ORDERS',
      'twap_status':'NOT_EVALUATED_5MIN_DEPTH_SNAPSHOTS_TOO_SPARSE',
      'tradeable_alpha_confirmed':False,'real_orders_enabled':False,
      'blind_oos_status':'NOT_OPENED_NEW_INDEPENDENT_MULTI_DAY_DATA_REQUIRED',
      'research_verdict':'INSUFFICIENT_EVIDENCE',
      'trial_history':[], 'aborted_or_failed_trials':[],
      'latency_scenarios_ms':[0,250,1000],
      'latency_sensitivity_status':'NOT_EVALUATED_SINGLE_SNAPSHOT_NOT_DELAYED_L2',
    }
    return result


def write_report(source=BASE_ROOT, dest=DEST):
    obj=build(source)
    raw=(json.dumps(obj,sort_keys=True,separators=(',',':'))+'\n').encode()
    dest=Path(dest);dest.parent.mkdir(parents=True,exist_ok=True)
    if dest.exists() or dest.is_symlink() or dest.with_name('report.sha256.json').exists():
        raise FactorLabReject('Write-once report already exists')
    pin={k:obj[k] for k in ('schema','experiment_id','source_manifest_sha256','v30_projection_sha256','v34_depth_projection_sha256')}
    pin['report_sha256']=sha256(raw).hexdigest();pin['size_bytes']=len(raw)
    # Never overwrite the immutable original; output is confined to Q7 paths.
    with dest.open('xb') as f:f.write(raw)
    dest.with_name('report.sha256.json').write_text(json.dumps(pin,indent=2,sort_keys=True)+'\n')
    return {'sha256':pin['report_sha256'],'bytes':len(raw),'summary':obj['sample_summary']}


def load_report():
    if DEST.is_symlink() or PIN.is_symlink():raise FactorLabReject('Unsafe factor report')
    if not DEST.is_file() or not PIN.is_file():raise FactorLabReject('No pinned Factor Lab report')
    raw=DEST.read_bytes();pin=_strict_json(PIN.read_bytes())
    if len(raw)>3_000_000 or len(raw)!=pin['size_bytes'] or sha256(raw).hexdigest()!=pin['report_sha256']:
        raise FactorLabReject('Q7 source report pin mismatch')
    obj=_strict_json(raw)
    if obj.get('preregistration_sha256')!=preregistration_digest():
        raise FactorLabReject('Preregistration changed after factor research')
    baseline,digest=load_projection();l2,l2digest,_,_=load_l2()
    if (obj['schema']!=SCHEMA or obj['experiment_id']!=RESEARCH_ID or
        obj['source_manifest_sha256']!=baseline['source_manifest_sha256'] or
        obj['v30_projection_sha256']!=digest or obj['v34_depth_projection_sha256']!=l2digest or
        (BASE_ROOT/'manifest.json').is_symlink() or
        sha256((BASE_ROOT/'manifest.json').read_bytes()).hexdigest()!=obj['source_manifest_sha256'] or
        obj['tradeable_alpha_confirmed'] is not False or obj['real_orders_enabled'] is not False):
        raise FactorLabReject('Q7 source identity or safety contract changed')
    return obj,pin['report_sha256']


def view(product,asof_ms,section):
    if product not in PRODS or section not in ('research','microstructure','execution'):
        raise FactorLabReject('Unknown request')
    if type(asof_ms) is not int:raise FactorLabReject('Missing as-of time')
    obj,digest=load_report()
    if not obj['source_start_receiver_ms']<=asof_ms<=obj['source_end_receiver_ms']:
        raise FactorLabReject('As-of outside archived source bounds')
    base={'schema':SCHEMA,'experiment_id':obj['experiment_id'],'product':product,
          'source_sha256':obj['source_manifest_sha256'],'report_sha256':digest,
          'mode':'ARCHIVE_REPLAY','oos_status':obj['blind_oos_status'],
          'verdict':obj['research_verdict'],'real_orders_enabled':False,
          'source_end_receiver_ms':obj['source_end_receiver_ms'],
          'asof_receiver_ms':asof_ms,'section':section}
    if section=='research':
        asof_signals=[s for s in obj['signals_with_future_labels_isolated'][product]
                      if s['decision_receiver_ms']<=asof_ms]
        summary=dict(obj['sample_summary'][product])
        summary['feature_rows']=len(asof_signals)
        for key,lag in [('primary_60s_future_mid_labels',60000),
                        ('auxiliary_5s_future_mid_labels',5000),('auxiliary_30s_future_mid_labels',30000)]:
            summary[key]=sum(s['future_only_diagnostic_labels'][str(lag)]['status'].startswith('HISTORICAL_')
                             and s['future_only_diagnostic_labels'][str(lag)]['available_after_receiver_ms']<=asof_ms
                             for s in asof_signals)
        base.update({'summary':summary,
            'features':list(next(iter(obj['signals_with_future_labels_isolated'][product]),{}).get('features_asof',{})),
            'primary_horizon_ms':PRIMARY_HORIZON_MS,
            'trial_budget':8,'trials_executed_on_blind':0,
            'predictive_effect':None,'cost_adjusted_pnl':None})
    elif section=='microstructure':
        sampled=[s for s in obj['signals_with_future_labels_isolated'][product] if s['decision_receiver_ms']<=asof_ms]
        # The future outcome is strictly withheld if its end time exceeds user as-of.
        base['rows']=[{'decision_receiver_ms':s['decision_receiver_ms'],'features_asof':s['features_asof'],
                        'source_age_ms':s['source_age_ms']} for s in sampled[-24:]]
        base['recovery_cohort']='POST_HOC_OUTCOMES_NOT_AVAILABLE_AT_SHOCK_TIME'
    else:
        depth=[s for s in obj['static_depth_cost_references'][product] if s['decision_receiver_ms']<=asof_ms]
        base['samples']=depth[-1:]  # bounded static book estimate, never a fill
        base['twap_status']=obj['twap_status']
    return base


def main():
    p=argparse.ArgumentParser();p.add_argument('--build',action='store_true');p.add_argument('--source',type=Path,default=BASE_ROOT);p.add_argument('--view',choices=['research','microstructure','execution']);p.add_argument('--product',default='BTC-USD');p.add_argument('--asof',type=int)
    a=p.parse_args()
    if a.build:print(json.dumps(write_report(a.source),indent=2))
    elif a.view:print(json.dumps(view(a.product,a.asof,a.view),indent=2))
    else:p.error('Specify --build or --view')

if __name__=='__main__':main()
