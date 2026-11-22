"""Deterministic local engineering gates for the BTC/ETH read-only terminal.

This script never downloads data, connects to a broker, imports alpha results,
or changes the research source of truth. It does not claim end-to-end UI safety.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str], cwd: Path, env: dict | None = None,
        timeout_seconds: int = 120) -> dict:
    import time
    started = time.monotonic()
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          check=False, timeout=timeout_seconds, env=env)
    elapsed = time.monotonic() - started
    return {
        'duration_seconds': round(elapsed, 3),
        'command': ' '.join(cmd),
        'return_code': proc.returncode,
        'stdout_tail': proc.stdout[-1200:],
        'stderr_tail': proc.stderr[-1200:],
    }


def audit_local_registered_source() -> dict:
    """Audit archived HTTP bytes with both parsers; do not claim historical PIT."""
    sys.path.insert(0, str(ROOT / 'backend'))
    import fetch_btc_eth
    from independent_audit import audit_bundle_independently

    registered = ROOT / 'backend' / 'data' / 'coinbase_spot_5m_20261108T0000Z_20261109T0000Z'
    repeated = ROOT / 'backend' / 'data' / 'repeat_captures' / registered.name
    if not registered.exists():
        return {'status': 'NO_REAL_SOURCE_ARCHIVE', 'verified': False,
                'independent_exchange_corroboration': False,
                'historical_point_in_time_proven': False}
    try:
        primary = fetch_btc_eth.verify_bundle(registered)
        independent = audit_bundle_independently(registered)
        same_provider_repeat = False
        if repeated.is_dir():
            repeat_primary = fetch_btc_eth.verify_bundle(repeated)
            repeat_independent = audit_bundle_independently(repeated)
            same_provider_repeat = all(
                primary['markets'][product]['sha256'] ==
                repeat_primary['markets'][product]['sha256'] and
                independent['markets'][product]['csv_sha256'] ==
                repeat_independent['markets'][product]['csv_sha256']
                for product in ('BTC-USD', 'ETH-USD')
            )
            # Byte equality is stronger than normalized price equality for this case.
            for product in ('btc', 'eth'):
                stem = product + '_coinbase_spot_5m'
                for filename in (stem + '.csv', f'raw/{stem}/0000.json'):
                    if (registered / filename).read_bytes() != (repeated / filename).read_bytes():
                        same_provider_repeat = False
        passed = (primary['verified'] and
                  independent['status'] == 'INTERNAL_CONSISTENCY_PASSED' and
                  same_provider_repeat)
        return {'status': 'DOUBLE_AUDITED_PROVIDER_RECAPTURE_MATCH' if passed
                else 'ARCHIVE_OR_REPEAT_INCOMPLETE',
                'verified': passed, 'matched_two_acquisitions': same_provider_repeat,
                'markets': primary['markets'],
                'archive_origin':'public Coinbase HTTPS requests through GitHub Actions; no separately signed record',
                'independent_exchange_corroboration':False,
                'historical_point_in_time_proven':False,
                'actual_live_l2_capture_proven':False}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'status': 'SOURCE_AUDIT_FAILED', 'verified': False,
                'error': str(exc)[:500], 'historical_point_in_time_proven':False}


def audit_local_public_l2() -> dict:
    """Require both saved live source sessions, each with two independent replayers.

    Distinct capture times are not exchange-side reference book verification.
    """
    sys.path.insert(0, str(ROOT / 'backend'))
    from audit_l2_pilot import audit_session
    from audit_l2_deep import deep_replay
    samples = {
        '20s_original_20261113': ROOT / 'backend' / 'data' / 'l2_pilot_20261113_205058UTC',
        '35s_repeat_20261113': ROOT / 'backend' / 'data' / 'l2_pilot_second_35s_20261113',
    }
    results = {}
    for name, sample in samples.items():
        if not sample.is_dir():
            return {'status':'MISSING_L2_CAPTURE','verified':False,'missing':name}
        try:
            primary=audit_session(sample)
            independent=deep_replay(sample)
            same=primary['source_text_stream_sha256']==independent['input_stream_sha256']
            for product in ('BTC-USD','ETH-USD'):
                same=same and (
                    primary['per_product'][product]['best_bid_final']==
                    independent['per_product'][product]['best_bid_final'] and
                    primary['per_product'][product]['best_ask_final']==
                    independent['per_product'][product]['best_ask_final'] and
                    primary['per_product'][product]['individual_trades_reported']==
                    independent['per_product'][product]['source_reported_trades_total'])
            results[name] = {'verified':bool(same),
                'raw_payload_stream_sha256':independent['input_stream_sha256'],
                'connection_sequence_range':[independent['connection_sequence_first'],
                                              independent['connection_sequence_last']],
                'messages':independent['total_messages'],
                'heartbeat_events':independent['heartbeat_events'],
                'incremental_trade_records':{
                    p:independent['per_product'][p]['incremental_trade_records']
                    for p in ('BTC-USD','ETH-USD')},
                'original_collector_trade_telemetry_mismatch':{
                    p:independent['collector_telemetry'][p]['capture_telemetry_mismatch']
                    for p in ('BTC-USD','ETH-USD')}}
        except (OSError,ValueError,KeyError,TypeError) as exc:
            return {'status':'L2_REPLAY_REJECTED','verified':False,'failed_session':name,
                    'error':str(exc)[:500]}
    verified=all(x['verified'] for x in results.values())
    return {
        'status':'TWO_SOURCE_SESSIONS_DOUBLE_INTERNALLY_REPLAYED' if verified
                 else 'REPLAY_DISAGREEMENT',
        'verified':verified,
        'sessions':results,
        'independent_exchange_reference_book_verified':False,
        'source_clock_synchronization_verified':False,
        'disconnection_or_recovery_verified':False,
        'historical_point_in_time_market_data_availability_verified':False,
        'trading_costs_or_profitability_verified':False,
        'no_orders_sent':True,
    }


def audit_real_120_second_public_source() -> dict:
    """Double-replay real bounded capture; exact trade replays are deduplicated.

    One 120-second window is NOT a long-soak or a deployable execution datafeed.
    """
    sys.path.insert(0, str(ROOT / 'backend'))
    from audit_l2_deep import deep_replay
    from audit_l2_pilot import audit_session
    source = ROOT / 'backend' / 'data' / 'l2_pilot_120s_20261113'
    if not source.exists():
        return {'status': 'SOURCE_MISSING', 'verified': False}
    try:
        a = deep_replay(source, allow_exact_trade_replays=True)
        b = audit_session(source, allow_exact_trade_replays=True)
        ok = (a['input_stream_sha256'] == b['source_text_stream_sha256']
              and a['total_messages'] == b['received_message_count']
              and a['total_messages'] == 4451
              and a['identical_trade_replays_excluded'].get('BTC-USD') == 5)
        for p in ('BTC-USD','ETH-USD'):
            ok = ok and (
                a['per_product'][p]['best_bid_final'] == b['per_product'][p]['best_bid_final']
                and a['per_product'][p]['best_ask_final'] == b['per_product'][p]['best_ask_final']
                and a['per_product'][p]['unique_trade_id_count'] == b['per_product'][p]['unique_trade_ids']
                and a['per_product'][p]['identical_update_trade_replays_excluded'] ==
                    b['per_product'][p]['identical_update_replays_excluded'])
        return {'status':'120S_INTERNAL_SOURCE_PARITY_ONLY' if ok else 'REPLAY_MISMATCH',
                'verified':bool(ok), 'messages':a['total_messages'],
                'raw_payload_sha256':a['input_stream_sha256'],
                'duplicate_update_trade_replays_excluded':a['identical_trade_replays_excluded'],
                'connected_trade_records_unique':{p:a['per_product'][p]['unique_trade_id_count']
                                                   for p in ('BTC-USD','ETH-USD')},
                'continuous_24h_soak_verified':False,
                'venue_book_independent_truth_verified':False,
                'source_time_external_clock_sync_verified':False,
                'executable_alpha_verified':False}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'status':'120S_REPLAY_REJECTED', 'verified':False,
                'error':str(exc)[:500]}


def audit_real_v11_chunked_capture() -> dict:
    """Independent source-only replay and no-fill quote audits of two real runs."""
    sys.path.insert(0, str(ROOT / 'backend'))
    from audit_soak import audit
    from independent_soak_replay import replay
    from quote_spread_audit import inspect
    runs = (
        ('initial', 'live_chunked_180s_20261114/coinbase_btc_eth_soak_5XeK6R', 7215, 3),
        ('reverified', 'live_chunked_reverified_180s_20261114/coinbase_btc_eth_soak_5BJxvi', 8742, 7),
    )
    checked = {}
    try:
        for name, subdir, expected_messages, expected_chunks in runs:
            source = ROOT / 'backend/data' / subdir
            if not source.is_dir():
                return {'verified': False, 'status': 'MANDATORY_REAL_SOURCE_MISSING',
                        'run': name}
            primary = audit(source)
            independent = replay(source)
            quotes = inspect(source, step_seconds=5, max_quote_age_seconds=3)
            products = ('BTC-USD', 'ETH-USD')
            same_counts = all(all(primary['per_product'][p].get(k, 0) == value
                                  for k, value in independent['per_product'][p].items())
                              for p in products)
            matching = (primary['status'] == 'INTERNALLY_REPLAYED_NO_KNOWN_GAP'
                        and primary['message_count'] == independent['messages'] == expected_messages
                        and primary['chunk_count'] == independent['chunks'] == expected_chunks
                        and primary['connection_sequences'] == {'0': [0, expected_messages - 1]}
                        and primary['books_final'] == independent['books_final']
                        and same_counts
                        and quotes['status'] == 'OBSERVED_QUOTES_ONLY_NO_FILL_PROOF'
                        and quotes['source_messages'] == expected_messages
                        and not quotes['actual_order_fills_verified']
                        and not quotes['historical_fee_tier_verified'])
            if name == 'reverified':
                matching = (matching and
                            primary['per_product']['BTC-USD'].get('identical_update_trade_replays_excluded') == 11)
            checked[name] = {
                'verified': bool(matching),
                'messages': primary['message_count'],
                'chunks': primary['chunk_count'],
                'books_final': primary['books_final'],
                'unique_trade_ids': {p: primary['per_product'][p]['unique_trade_id'] for p in products},
                'btc_identical_trade_retransmits_excluded':
                    primary['per_product']['BTC-USD'].get('identical_update_trade_replays_excluded', 0),
                'observed_spread_bps_median':
                    {p: quotes['products'][p]['spread_bps_median'] for p in products},
            }
        success = all(r['verified'] for r in checked.values()) and len(checked) == len(runs)
        return {
            'verified': bool(success),
            'status': 'TWO_180S_REAL_CHUNKED_SOURCE_DOUBLE_REPLAYED_NO_FILL_CLAIM'
                      if success else 'AUDIT_DISAGREEMENT',
            'source_sessions': checked,
            'continuous_24_hour_capture_verified': False,
            'independent_exchange_reference_verified': False,
            'historical_fee_tier_verified': False,
            'actual_execution_verified': False,
            'alpha_verified': False,
            'orders_enabled': False,
        }
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'verified': False, 'status': 'SOURCE_OR_MODEL_REJECTED',
                'error': str(exc)[:300]}


def audit_real_v12_disk_safe_capture() -> dict:
    """Producer disk-budget telemetry is checked against archived JSONL bytes.

    This is same-provider internal evidence, NOT authenticated independent
    market truth, 24h operational safety, clock sync or execution PnL.
    """
    sys.path.insert(0, str(ROOT / 'backend'))
    from audit_soak import audit
    from independent_soak_replay import replay
    from inspect_crashed_soak import inspect
    source = ROOT / 'backend' / 'data' / 'live_disk_safe_180s_20261114' / 'coinbase_btc_eth_soak_CWFeG3'
    if not source.is_dir():
        return {'verified': False, 'status': 'V12_MANDATORY_SOURCE_MISSING'}
    try:
        primary = audit(source)
        independent = replay(source)
        forensic = inspect(source)
        metadata = json.loads((source / 'manifest.json').read_bytes())
        telemetry = metadata.get('resource_telemetry') or {}
        matched = (primary['status'] == 'INTERNALLY_REPLAYED_NO_KNOWN_GAP'
                   and primary['message_count'] == independent['messages'] == 7264
                   and primary['chunk_count'] == independent['chunks'] == 3
                   and primary['connection_sequences'] == {'0': [0, 7263]}
                   and primary['books_final'] == independent['books_final']
                   and forensic['committed_chunks'] == 3
                   and not forensic['eligible_for_point_in_time_feature_research']
                   and telemetry.get('archived_jsonl_bytes') == sum(
                       c['archived_line_bytes'] for c in metadata['chunks'])
                   and telemetry.get('disk_checks') == 7264
                   and not metadata.get('faults'))
        for product in ('BTC-USD', 'ETH-USD'):
            matched = matched and all(
                primary['per_product'][product].get(k, 0) == v
                for k, v in independent['per_product'][product].items())
        return {'verified': bool(matched),
                'status': 'INTERNAL_SOURCE_AND_STORAGE_DOUBLE_AUDIT_ONLY' if matched else 'SOURCE_PARITY_FAILED',
                'messages': primary['message_count'],
                'chunks': primary['chunk_count'],
                'books_final': primary['books_final'],
                'resource_telemetry_reported_by_producer': telemetry,
                'real_github_capture_url': 'https://github.com/minseogiiiii/quant-research-system/actions/runs/43562190726',
                'independent_market_data_truth_verified': False,
                'continuous_24_hour_uptime_verified': False,
                'real_order_execution_verified': False,
                'executable_alpha_verified': False}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'verified': False, 'status': 'SOURCE_REPLAY_OR_STORAGE_REJECTED',
                'error': str(exc)[:500]}


def audit_real_v12_extended_capture() -> dict:
    """Require 10-minute and final collector-revision real archives, no orders.

    Two replay programs share Coinbase original source, so they cannot attest
    exchange-side independent book truth, clock sync or trading profitability.
    """
    sys.path.insert(0, str(ROOT / 'backend'))
    from audit_soak import audit
    from independent_soak_replay import replay
    cases = (
        ('ten_minute','live_disk_safe_600s_20261114/coinbase_btc_eth_soak_yRodga',24209,8,600),
        ('final_revision_180s','live_disk_safe_final_180s_20261114/coinbase_btc_eth_soak_rV6Xox',7253,3,180),
    )
    results={}
    try:
        for name, subdir, n, chunks, seconds in cases:
            src=ROOT/'backend/data'/subdir
            if not src.is_dir():
                return {'verified':False,'status':'MISSING_REAL_V12_SOURCE','case':name}
            a=audit(src)
            b=replay(src)
            m=json.loads((src/'manifest.json').read_bytes())
            t=m.get('resource_telemetry') or {}
            ok=(a['status']=='INTERNALLY_REPLAYED_NO_KNOWN_GAP'
                and a['message_count']==b['messages']==n
                and a['chunk_count']==b['chunks']==chunks
                and a['connection_sequences']=={'0':[0,n-1]}
                and a['books_final']==b['books_final']
                and m.get('required_duration_seconds')==seconds
                and m.get('finish_reason')=='DURATION_REACHED'
                and m.get('total_reconnects')==0
                and m.get('faults')==[]
                and t.get('archived_jsonl_bytes')==sum(c['archived_line_bytes'] for c in m['chunks'])
                and t.get('disk_checks')==n)
            for product in ('BTC-USD','ETH-USD'):
                ok=ok and all(a['per_product'][product].get(k,0)==v for k,v in
                              b['per_product'][product].items())
            results[name]={'verified':bool(ok),'messages':n,'chunks':chunks,
                           'observed_duration_seconds':seconds,
                           'recorded_jsonl_bytes':t.get('archived_jsonl_bytes'),
                           'per_product':a['per_product'],'books_final':a['books_final'],
                           'exchange_clock_sync':False,
                           'independent_book_truth':False}
        all_good=all(x['verified'] for x in results.values())
        return {'verified':all_good,
                'status':'V12_LONGER_REAL_CAPTURE_INTERNAL_REPLAY_PASSED' if all_good
                         else 'V12_INDEPENDENT_REPLAY_DISAGREEMENT',
                'sessions':results,
                'continuous_24hour_source_validated':False,
                'order_execution_or_alpha_verified':False}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'verified':False,'status':'V12_SOURCE_REJECTED','error':str(exc)[:500]}



def audit_real_v13_rolling_capture() -> dict:
    """Require two separately collected BTC/ETH public source captures.

    Two independent local parsers agree on source content, NOT an exchange-signed
    source, externally synchronized clock, execution quality, or OOS alpha.
    """
    sys.path.insert(0,str(ROOT/'backend'))
    from audit_soak import audit
    from independent_soak_replay import replay
    samples=(
      ('first','live_rolling_v13_120s_20261114/coinbase_btc_eth_soak_TX3aYK',4387,17,524288,
       'https://github.com/minseogiiiii/quant-research-system/actions/runs/43565275600',
       '8097e28868b7489fd7708669e6b803f82b810224'),
      ('final_code','live_rolling_v13_final_120s_20261114/coinbase_btc_eth_soak_mdkeBG',4438,20,524288,
       'https://github.com/minseogiiiii/quant-research-system/actions/runs/43566163912',
       '90b211e2a68f7607f5e1841df82080dbcf47609f'),
    )
    results={}
    try:
        for name,subdir,expected_messages,expected_chunks,chunk_target,url,commit in samples:
            source=ROOT/'backend/data'/subdir
            if not source.is_dir():
                return {'verified':False,'status':'MISSING_V13_RAW_SOURCE','case':name}
            a=audit(source)
            b=replay(source)
            m=json.loads((source/'manifest.json').read_text())
            t=m.get('resource_telemetry') or {}
            ok=(a['status']=='INTERNALLY_REPLAYED_NO_KNOWN_GAP'
                and a['message_count']==b['messages']==expected_messages
                and a['chunk_count']==b['chunks']==expected_chunks
                and a['connection_sequences']=={'0':[0,expected_messages-1]}
                and a['books_final']==b['books_final']
                and m.get('producer_release')=='v13_rolling_bounded'
                and m['rolling_policy'].get('chunk_target_bytes')==chunk_target
                and m.get('required_duration_seconds')==120
                and m.get('finish_reason')=='DURATION_REACHED'
                and m.get('total_reconnects')==0
                and m.get('faults')==[]
                and t.get('archived_jsonl_bytes')==sum(c['archived_line_bytes'] for c in m['chunks']))
            for symbol in ('BTC-USD','ETH-USD'):
                ok=ok and all(a['per_product'][symbol].get(k,0)==v for k,v
                              in b['per_product'][symbol].items())
            results[name]={
               'verified':bool(ok), 'messages':a['message_count'],
               'chunks':a['chunk_count'], 'book_hashes':{s:a['books_final'][s]['sha256'] for s in ('BTC-USD','ETH-USD')},
               'github_action':url, 'source_commit':commit,
               'producer_recorded_jsonl_bytes':t.get('archived_jsonl_bytes'),
            }
        valid=all(x['verified'] for x in results.values())
        return {'verified':valid,
                'status':'TWO_V13_SOURCE_SESSIONS_DOUBLE_REPLAYED' if valid else 'V13_SOURCE_PARITY_FAILURE',
                'sessions':results,
                'one_hour_continuous_source_verified':False,
                'independent_provider_book_truth_verified':False,
                'actual_execution_or_alpha_verified':False,
                'orders_enabled':False}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'verified':False,'status':'V13_ARCHIVE_AUDIT_REJECTED','error':str(exc)[:500]}



def audit_read_only_v15_taker_quote_envelope() -> dict:
    """Pin three preselected source-sequence quote calculations to real L2.

    These are indicative delayed *quote observations*, not fills, latency
    measurements, a backtest return series, broker account fees, or alpha.
    """
    sys.path.insert(0, str(ROOT / 'backend'))
    from taker_quote_envelope import Assumptions, evaluate
    source = ROOT / 'backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy'
    observations = [
        {'id':'btc_l2_seq100','product_id':'BTC-USD','side':'BUY',
         'quantity':'0.001','signal_sequence':100},
        {'id':'eth_l2_seq1200','product_id':'ETH-USD','side':'SELL',
         'quantity':'0.02','signal_sequence':1200},
        {'id':'btc_l2_seq2500','product_id':'BTC-USD','side':'BUY',
         'quantity':'0.005','signal_sequence':2500},
    ]
    try:
        outcome = evaluate(source, observations, Assumptions(250,5000,2500,'10','5','0.25'))
        expected = [
            ('btc_l2_seq100',116,'67198.472445'),
            ('eth_l2_seq1200',1207,'1895.061995'),
            ('btc_l2_seq2500',2509,'67231.418910'),
        ]
        actual = [(r['id'],r.get('quote_sequence'),r.get('hypothetical_quoted_price'))
                  for r in outcome['results']]
        ok = (actual == expected and outcome['source_messages_audited'] == 5177
              and all(r['status'] == 'INDICATIVE_TAKER_QUOTE_ONLY'
                      for r in outcome['results'])
              and outcome['source_manifest_sha256'] ==
                 '8f2e0ec88a601a92ab4c7bcd0e04b20536fbe46bbd8b3c7f67ae3c1d075c28e5'
              and outcome['fills_verified'] is False
              and outcome['strategy_alpha_verified'] is False
              and outcome['trading_or_api_writes_enabled'] is False)
        return {'verified':bool(ok),
                'status':'READ_ONLY_L2_QUOTE_ENVELOPE_REPRODUCED_NOT_FILLS' if ok
                         else 'P1_QUOTE_EVIDENCE_MISMATCH',
                'source_messages':outcome['source_messages_audited'],
                'fixed_observations':actual,
                'assumptions':{'hypothetical_latency_ms':250,
                               'hypothetical_one_way_taker_fee_bps':'10',
                               'hypothetical_slippage_bps':'5',
                               'max_best_level_capacity_fraction':'0.25'},
                'verified_venue_fills':False,'independent_exchange_clock':False,
                'economic_alpha':False,'broker_orders_enabled':False}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'verified':False,'status':'P1_SOURCE_AUDIT_REJECTED',
                'error':str(exc)[:500]}


def audit_v16_one_hour_and_sensitivity() -> dict:
    """Mandatory pinned 1h source double replay; sensitivity is *not* alpha."""
    sys.path.insert(0, str(ROOT / 'backend'))
    from audit_v16_one_hour import audit_one_hour, SOURCE_MANIFEST_SHA
    import hashlib
    try:
        src=ROOT/'backend/data/live_v16_one_hour/coinbase_btc_eth_soak_eE9SNH'
        one_hour=audit_one_hour(src)
        scenarios=json.loads((ROOT/'examples/v16_predeclared_cost_scenarios.json').read_bytes())
        intents=json.loads((ROOT/'examples/v16_predeclared_one_hour_intents.json').read_bytes())
        report=json.loads((ROOT/'P1_ONE_HOUR_SENSITIVITY_v16.json').read_bytes())
        intent_hash=hashlib.sha256(json.dumps(intents,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        scenario_hash=hashlib.sha256(json.dumps(scenarios,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if not (report.get('status')=='PREDECLARED_HYPOTHETICAL_COST_SENSITIVITY_NO_ORDERS'
                and report['source_manifest_sha256']==SOURCE_MANIFEST_SHA
                and report['frozen_intent_declaration_sha256']==intent_hash
                and report['frozen_scenario_declaration_sha256']==scenario_hash
                and report['scenario_count']==4
                and report['observation_count']==5
                and report['best_scenario_selected'] is False
                and report['actual_order_sent'] is False
                and report['actual_fills_verified'] is False
                and report['profitability_or_alpha_verified'] is False):
            raise ValueError('One-hour P1 preregistered sensitivity report incomplete/changed')
        expected = {
            'baseline': [1013,20013,60009,100010,150013],
            'higher_fee_slippage':[1013,20013,60009,100010,150013],
            'slower_observation':[1051,20045,60045,100037,150040],
            'thin_visible_depth':[1013,20013,60009,100010,150013],
        }
        for item in report['scenario_matrix']:
            label=item['scenario']
            if (label not in expected or
                [o.get('quote_sequence') for o in item['observations']]!=expected[label] or
                item['indicative_quote_count'] != (3 if label=='thin_visible_depth' else 5) or
                item['rejected_count'] != (2 if label=='thin_visible_depth' else 0)):
                raise ValueError('One-hour scenario result differs from pinned source replay')
        if set(x['scenario'] for x in report['scenario_matrix'])!=set(expected):
            raise ValueError('Missing preregistered stress scenario')
        return {'verified':True,'status':'V16_ONE_HOUR_SOURCE_AND_P1_SENSITIVITY_VERIFIED',
                'one_hour_source_audit':one_hour,
                'scenario_count':4,'fixed_observation_count':5,
                'fees_or_fills_verified':False,'alpha_verified':False}
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return {'verified':False,'status':'V16_ONE_HOUR_OR_SENSITIVITY_REJECTED',
                'error':str(exc)[:500], 'alpha_verified':False}


def audit_v17_static_depth() -> dict:
    """Mandatory same-source frozen static-depth report, never verified fills."""
    sys.path.insert(0, str(ROOT / 'backend'))
    from audit_v17_depth import verify
    try:
        return verify()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'verified':False,'status':'V17_STATIC_DEPTH_AUDIT_REJECTED',
                'error':str(exc)[:500], 'actual_fill_verified':False,
                'alpha_verified':False}


def audit_v18_future_only_markout() -> dict:
    """Recompute frozen 1-hour future labels, never as trading features."""
    sys.path.insert(0,str(ROOT / 'backend'))
    from audit_v18_markout import verify
    try:
        return verify()
    except (OSError,ValueError,KeyError,TypeError) as exc:
        return {'status':'V18_FUTURE_LABEL_AUDIT_REJECTED',
                'verified':False, 'error':str(exc)[:500],
                'fills_verified':False, 'alpha_verified':False}


def validate() -> dict:
    tasks = {
        'python_tests': run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q'], ROOT,
                            timeout_seconds=360),
        'python_compile': run([sys.executable, '-m', 'compileall', '-q', 'backend', 'tests', 'scripts'], ROOT),
    }
    frontend = ROOT / 'frontend'
    if shutil.which('tsc'):
        tasks['typescript_contract'] = run(['tsc', '--noEmit', '--project', 'tsconfig.contract.json'], frontend)
    else:
        tasks['typescript_contract'] = {'status': 'NOT_RUN', 'reason': 'tsc unavailable'}
    if shutil.which('node'):
        node_env = os.environ.copy()
        # The dependency is NOT installed or downloaded here. If a global
        # TypeScript installation exists, enable its module search path.
        if shutil.which('npm') and not node_env.get('NODE_PATH'):
            npm_root = subprocess.run(['npm', 'root', '-g'], capture_output=True,
                                      text=True, check=False, timeout=10)
            if npm_root.returncode == 0:
                node_env['NODE_PATH'] = npm_root.stdout.strip()
        tasks['frontend_syntax'] = run(['node', 'check-syntax.cjs'], frontend, env=node_env)
    else:
        tasks['frontend_syntax'] = {'status': 'NOT_RUN', 'reason': 'node unavailable'}
    if shutil.which('node'):
        tasks['l2_capture_js_syntax'] = run(['node', '--check', 'scripts/btc_eth_public_l2_pilot.mjs'], ROOT)
        tasks['v10_soak_capture_js_syntax'] = run(['node', '--check', 'scripts/btc_eth_soak_capture.mjs'], ROOT)
        tasks['v11_live_chunk_js_syntax'] = run(['node', '--check', 'scripts/btc_eth_live_chunked_probe.mjs'], ROOT)
        tasks['v13_rolling_js_syntax'] = run(['node', '--check', 'scripts/btc_eth_rolling_v13.mjs'], ROOT)
        tasks['v14_fault_guard_js_syntax'] = run(['node', '--check', 'scripts/btc_eth_rolling_v14.mjs'], ROOT)
    v15_quote_envelope = audit_read_only_v15_taker_quote_envelope()
    v16_one_hour = audit_v16_one_hour_and_sensitivity()
    v17_depth = audit_v17_static_depth()
    v18_markout = audit_v18_future_only_markout()
    manifest = json.loads((ROOT / 'backend' / 'manifest.json').read_text())
    source = audit_local_registered_source()
    l2 = audit_local_public_l2()
    real_120s = audit_real_120_second_public_source()
    live_v11 = audit_real_v11_chunked_capture()
    live_v12 = audit_real_v12_disk_safe_capture()
    live_v12_extended = audit_real_v12_extended_capture()
    live_v13 = audit_real_v13_rolling_capture()
    sys.path.insert(0, str(ROOT / 'backend'))
    from audit_v14_recovery import audit_recordings
    try:
        live_v14 = audit_recordings(ROOT)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        live_v14 = {'status':'V14_SOURCE_REJECTED','verified':False,'error':str(exc)[:500]}
    sys.path.insert(0, str(ROOT / 'backend'))
    from audit_l2_reconnect import audit_reconnect
    try:
        reconnect = audit_reconnect(ROOT / 'backend' / 'data' / 'reconnect_evidence')
        reference_probes = [r for segment in reconnect['segments']
                            for r in segment['reference_books']]
        four_refs = (len(reference_probes) == 4 and
                     all(r['availability'] == 'ARCHIVED_SOURCE_VALID' and
                         r['comparison']['rest_bid_in_observed_ws_range'] and
                         r['comparison']['rest_ask_in_observed_ws_range']
                         for r in reference_probes))
        reconnect['four_rest_reference_time_window_correlations_observed'] = four_refs
        reconnect['verified'] = (reconnect['book_admission_gate_completed_connections']==2
                                 and reconnect['book_admission_gate_invalidated_connections']==2
                                 and four_refs)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        reconnect = {'status': 'RECONNECT_OR_REFERENCE_SOURCE_AUDIT_FAILED',
                     'verified': False, 'error': str(exc)[:500]}

    return {
        'project': 'BTC_ETH_SPOT_5M_RESEARCH_ONLY',
        'release': 'v18',
        'v18_future_only_markout_verified':v18_markout.get('verified',False),
        'v18_future_only_markout_evidence':v18_markout,
        'v18_observed_price_markout_is_not_a_fill':True,
        'v18_real_maker_queue_and_partial_fills_verified':False,
        'v17_static_depth_stress_verified':v17_depth.get('verified',False),
        'v17_static_depth_stress_evidence':v17_depth,
        'v17_static_l2_depth_is_not_fill':True,
        'v16_one_hour_live_source_verified':v16_one_hour['verified'],
        'v16_one_hour_and_predeclared_p1_evidence':v16_one_hour,
        'v16_24h_live_source_verified':False,
        'v16_actual_fee_or_fill_verified':False,
        'v15_read_only_taker_quote_envelope_verified': v15_quote_envelope['verified'],
        'v15_read_only_taker_quote_envelope_evidence': v15_quote_envelope,
        'v15_broker_fee_and_latency_measured': False,
        'v15_actual_fills_or_pnl_proven': False,
        'v15_one_hour_live_source_verified': False,
        'v10_soak_archive_schema': 'btc_eth_raw_soak_v10',
        'v10_soak_error_injection_suite_included': True,
        'v10_continuous_market_capture_soak_passed': False,
        'v11_180s_live_chunked_source_double_replay_passed': live_v11.get('verified',False),
        'v11_180s_live_chunked_evidence': live_v11,
        'v12_180s_live_disk_safe_source_double_replay_passed': live_v12.get('verified',False),
        'v12_180s_live_disk_safe_source_evidence': live_v12,
        'v12_ten_minute_continuous_source_verified': live_v12_extended.get('verified',False),
        'v12_600s_and_final_revision_180s_real_evidence': live_v12_extended,
        'v13_rolling_120s_source_double_audit_verified':live_v13.get('verified',False),
        'v13_rolling_120s_evidence':live_v13,
        'v13_one_hour_live_source_verified':False,
        'v14_controlled_disconnect_double_replay_passed':live_v14.get('verified',False),
        'v14_read_only_source_recovery_evidence':live_v14,
        'v14_unplanned_network_outage_proven':False,
        'v14_one_hour_live_source_proven':False,
        'v12_continuous_24h_capture_verified': False,
        'v10_24_hour_soak_passed': False,
        'v10_recovery_after_unplanned_network_loss_proven': False,
        'market_data_registered': len(manifest.get('datasets', [])),
        'actual_provider_historical_http_response_archived_and_audited': source.get('verified', False),
        'provider_historical_capture_run_urls': [
            'https://github.com/minseogiiiii/quant-research-system/actions/runs/43503215243',
            'https://github.com/minseogiiiii/quant-research-system/actions/runs/43503528152'],
        'source_archive_audit': source,
        'public_l2_internal_audit': l2,
        'public_l2_real_120s_double_audit': real_120s,
        'public_l2_controlled_reconnect_audit': reconnect,
        'mandatory_reconnect_audit_pass': reconnect.get('verified',False),
        'mandatory_l2_audit_pass': l2.get('verified',False),
        'mandatory_120s_source_audit_pass': real_120s.get('verified',False),
        'results': tasks,
        'mandatory_python_pass': all(tasks[k]['return_code'] == 0 for k in
                                     ('python_tests', 'python_compile')),
        'mandatory_real_archive_pass_if_registered':
            not manifest.get('datasets') or source.get('verified', False),
        'frontend_contract_run': tasks['typescript_contract'].get('return_code') == 0,
        'frontend_syntax_run': tasks['frontend_syntax'].get('return_code') == 0,
        'full_browser_build_verified': False,
        'coinbase_live_websocket_source_recorded': l2.get('verified',False),
        'coinbase_live_websocket_independent_book_truth_verified':False,
        'historical_point_in_time_proven': False,
        'executable_alpha_verified': False,
        'orders_enabled': False,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, help='Write JSON report, never a market dataset')
    args = p.parse_args()
    result = validate()
    rendered = json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + '\n'
    if args.output:
        args.output.write_text(rendered, encoding='utf-8')
    print(rendered)
    if (not result['mandatory_python_pass'] or not result['mandatory_real_archive_pass_if_registered']
            or not result['mandatory_l2_audit_pass']
            or not result['mandatory_120s_source_audit_pass']
            or not result['v11_180s_live_chunked_source_double_replay_passed']
            or not result['v12_180s_live_disk_safe_source_double_replay_passed']
            or not result['v12_ten_minute_continuous_source_verified']
            or not result['v13_rolling_120s_source_double_audit_verified']
            or not result['v14_controlled_disconnect_double_replay_passed']
            or not result['v17_static_depth_stress_verified']
            or not result['v18_future_only_markout_verified']
            or not result['v16_one_hour_live_source_verified']
            or not result['v15_read_only_taker_quote_envelope_verified']
            or not result['mandatory_reconnect_audit_pass']):
        raise SystemExit(1)
    if any(item.get('return_code', 0) != 0 for item in result['results'].values()):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
