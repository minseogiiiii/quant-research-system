"""Pinned v17 static depth stress replay for the previously verified one-hour archive.

This evidence gate uses exactly seven predeclared descriptive observations, not
strategy entries or training labels. No extra actual exchange validation or fill.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path

from static_depth_stress import diagnose, DepthPolicy
from taker_quote_envelope import Assumptions, unique_json
from audit_v16_one_hour import SOURCE_MANIFEST_SHA

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'backend/data/live_v16_one_hour/coinbase_btc_eth_soak_eE9SNH'
INTENTS = ROOT / 'examples/v17_predeclared_depth_intents.json'
POLICY = ROOT / 'examples/v17_predeclared_depth_policy.json'
RESULT = ROOT / 'P1_STATIC_DEPTH_STRESS_v17.json'
INTENTS_SHA = 'c7763f6107f332f83329210ca753861f53b5c0e62524481c899c9de05b29895c'
POLICY_SHA = 'de3edb756f7c3eaf37be9af90aa5bb92179fc8824e43abad85a1bb9aecfe72f6'
FROZEN_IDS = (
 'btc_buy_seq_1000', 'eth_sell_seq_20000', 'btc_depth_stress_30000',
 'btc_sell_seq_60000', 'eth_depth_stress_80000', 'eth_buy_seq_100000',
 'btc_buy_seq_150000',
)
FROZEN_QUOTES = (1013,20013,30015,60009,80013,100010,150013)

class DepthEvidenceError(ValueError):
    pass


def verify() -> dict:
    if sha256(INTENTS.read_bytes()).hexdigest() != INTENTS_SHA:
        raise DepthEvidenceError('Altered preregistered intents')
    if sha256(POLICY.read_bytes()).hexdigest() != POLICY_SHA:
        raise DepthEvidenceError('Altered preregistered policy')
    intents = unique_json(INTENTS.read_bytes())
    params = unique_json(POLICY.read_bytes())
    if (set(params) != {'max_levels','max_static_sweep_bps', 'latency_ms',
                        'max_wait_ms','max_signal_quote_age_ms',
                        'one_way_taker_fee_bps','extra_slippage_bps',
                        'top_level_capacity_fraction'}):
        raise DepthEvidenceError('Unknown policy field')
    assumptions = Assumptions(params['latency_ms'],params['max_wait_ms'],
         params['max_signal_quote_age_ms'],params['one_way_taker_fee_bps'],
         params['extra_slippage_bps'],params['top_level_capacity_fraction'])
    policy = DepthPolicy(params['max_levels'],params['max_static_sweep_bps'])
    actual = diagnose(SOURCE,intents,assumptions,policy)
    recorded = unique_json(RESULT.read_bytes())
    if actual != recorded:
        raise DepthEvidenceError('Replayed source changed compared with frozen depth report')
    results = actual['results']
    if (actual['source_manifest_sha256'] != SOURCE_MANIFEST_SHA
            or actual['source_messages_audited'] != 152069
            or len(results) != 7
            or tuple(r['id'] for r in results) != FROZEN_IDS
            or tuple(r['quote_sequence'] for r in results) != FROZEN_QUOTES
            or sum(r['status'] == 'STATIC_L2_DEPTH_REFERENCE_NOT_FILL' for r in results) != 6
            or results[4]['status'] != 'REJECTED_STATIC_DEPTH_NOT_DEMONSTRATED_WITHIN_LEVEL_LIMIT'
            or results[2]['original_quote_status'] != 'REJECTED_INSUFFICIENT_BEST_LEVEL_DEPTH'
            or actual['actual_fills_verified'] is not False
            or actual['actual_order_sent'] is not False
            or actual['market_impact_verified'] is not False):
        raise DepthEvidenceError('Static depth evidence semantics unexpectedly changed')
    return {'verified':True,
            'status':'V17_STATIC_BOOK_DEPTH_SOURCE_REPLAY_VERIFIED_NOT_FILL',
            'source_messages':actual['source_messages_audited'],
            'source_manifest_sha256':actual['source_manifest_sha256'],
            'frozen_intents_sha256':INTENTS_SHA,
            'frozen_policy_sha256':POLICY_SHA,
            'fixed_observation_count':7,
            'indicative_static_reference_count':6,
            'rejected_depth_count':1,
            'no_future_quote_selection':True,
            'market_impact_verified':False,
            'actual_fill_verified':False,
            'reliable_live_order_latency_verified':False,
            'alpha_verified':False,
            'orders_enabled':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path)
    args=p.parse_args()
    result=verify()
    raw=json.dumps(result,indent=2,sort_keys=True)+'\n'
    if args.output:args.output.write_text(raw)
    print(raw)

if __name__=='__main__':main()
