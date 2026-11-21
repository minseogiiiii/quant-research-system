"""v20: independently reconcile preserved v19-A and v19-B tape diagnostics.

READ ONLY. No fills, trading, inferred historical availability or OOS use.
This script does not import either v19 maker engine. It checks immutable original
chunk bytes, packet digests, sequence and duplicate trade IDs, then recomputes
all intersections of time window, Coinbase MAKER-side filter and event-time gate.
The historical maker side is documented in Coinbase Advanced 2025-07-30 changelog.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path


def fail(message):
    raise ValueError(message)


def load(raw):
    def pairs(entries):
        ret = {}
        for k, v in entries:
            if k in ret:
                fail('Duplicate JSON member: ' + k)
            ret[k] = v
        return ret
    def constant(v):
        fail('Nonfinite JSON token ' + v)
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def utc(value):
    if not isinstance(value, str) or not value.endswith('Z'):
        fail('Unrecognized/non-UTC timestamp')
    d = datetime.fromisoformat(value[:-1] + '+00:00')
    if d.tzinfo is None or d.utcoffset().total_seconds() != 0:
        fail('Non-UTC timestamp')
    return d


def dec(value):
    if not isinstance(value, str):
        fail('Non-decimal-string numeric field')
    n = Decimal(value)
    if not n.is_finite() or n < 0:
        fail('Negative/nonfinite numeric field')
    return n


def reconcile(source: Path, report_a: dict, report_b: dict) -> dict:
    source = Path(source)
    if source.is_symlink() or (source / 'manifest.json').is_symlink():
        fail('Symlinked source')
    original_manifest = (source / 'manifest.json').read_bytes()
    root_hash = sha256(original_manifest).hexdigest()
    manifest = load(original_manifest)
    if (root_hash != report_a['source_manifest_sha256'] or root_hash != report_b['source_manifest_sha256']):
        fail('The releases use different source manifests')
    if (manifest.get('ready_for_research') is not True or len(manifest['connections']) != 1
            or manifest.get('faults') or len(manifest['chunks']) != 41):
        fail('Not the admitted one-hour source')
    if (report_a['resting_window_ms_predeclared'] != 30000
            or report_b['policy_horizon_ms_assumed'] != 5000):
        fail('Unexpected original frozen horizons')
    a = {x['id']: x for x in report_a['results'] if x['status'] == 'CONDITIONAL_FLOW_DIAGNOSTIC_COMPLETE'}
    b = {x['id']: x for x in report_b['results'] if x['status'] == 'CONDITIONAL_TAPE_CAPACITY_NOT_ORDER_FILL'}
    if len(a) != 7 or len(b) != 5 or not set(b).issubset(a):
        fail('Original candidate cohorts changed')
    for id_, ref in b.items():
        x = a[id_]
        if (x['anchor_sequence'] != ref['quote_sequence'] or x['side'] != ref['side']
                or x['product_id'] != ref['product_id']
                or dec(x['posted_price_hypothetical']) != dec(ref['hypothetical_resting_price_usd'])
                or dec(x['initial_visible_same_side_quantity']) != dec(ref['displayed_resting_level_base_at_quote'])):
            fail('Shared quote anchor does not match: ' + id_)
    anchors = {q['anchor_sequence']: q for q in a.values()}
    if len(anchors) != len(a):
        fail('Nonunique source anchor')
    states = {id_: {'receipt_ns': None, 'received_utc': None, 'counts': {}}
              for id_ in a}
    # Each window has four deliberately distinct raw-flow definitions.
    for s in states.values():
        for horizon in (5000, 30000):
            for side in ('all', 'maker'):
                for gate in ('recv', 'recv_and_event'):
                    s['counts'][f'{horizon}ms_{side}_{gate}'] = {'volume': Decimal(0), 'trades': 0,
                                                                  'pre_anchor_events_excluded': 0}
    known = {}
    seq = 0
    last_ns = -1
    snapshots = 0
    unique_updates = 0
    repeated = 0
    for chunk in manifest['chunks']:
        target = source / chunk['file']
        if (target.is_symlink() or not target.is_file()
                or not target.resolve().is_relative_to(source.resolve())):
            fail('Unsafe/missing chunk')
        blob = target.read_bytes()
        if len(blob) != chunk['archived_line_bytes'] or sha256(blob).hexdigest() != chunk['archive_sha256']:
            fail('Original source chunk hash/length mismatch')
        for line in blob.splitlines(keepends=True):
            if not line.endswith(b'\n'):
                fail('Truncated source line')
            row = load(line)
            raw = row['raw_text']
            if not isinstance(raw, str) or sha256(raw.encode('utf-8')).hexdigest() != row['sha256']:
                fail('Source payload changed')
            msg = load(raw)
            if type(msg.get('sequence_num')) is not int or msg['sequence_num'] != seq:
                fail('Packet sequence discontinuity')
            stamp = row['local_monotonic_ns']
            if not isinstance(stamp, str) or not stamp.isdecimal() or len(stamp) > 25:
                fail('Malformed receipt clock')
            ns = int(stamp)
            if ns < last_ns:
                fail('Monotonic clock regressed')
            last_ns = ns
            if seq in anchors:
                q = anchors[seq]
                s = states[q['id']]
                if row['received_utc'] != q['anchor_received_utc']:
                    fail('Anchor wall timestamp mismatch')
                s['receipt_ns'] = ns
                s['received_utc'] = utc(row['received_utc'])
            if msg['channel'] == 'market_trades':
                for event in msg['events']:
                    if event['type'] == 'snapshot':
                        snapshots += len(event['trades'])
                        continue
                    if event['type'] != 'update':
                        fail('Unrecognized market-trades event')
                    for trade in event['trades']:
                        product = trade['product_id']
                        if product not in ('BTC-USD', 'ETH-USD') or trade['side'] not in ('BUY', 'SELL'):
                            fail('Unknown trade product or side')
                        trade_id = trade['trade_id']
                        if not isinstance(trade_id,str) or not trade_id:
                            fail('Missing trade ID')
                        key = (product, trade_id)
                        sig = (trade['price'], trade['size'], trade['time'], trade['side'])
                        if key in known:
                            if known[key] != sig:
                                fail('Contradictory trade replay')
                            repeated += 1
                            continue
                        known[key] = sig
                        unique_updates += 1
                        price = dec(trade['price'])
                        size = dec(trade['size'])
                        event_time = utc(trade['time'])
                        for id_, q in a.items():
                            s = states[id_]
                            if (s['receipt_ns'] is None or product != q['product_id']
                                    or price != dec(q['posted_price_hypothetical'])
                                    or not s['receipt_ns'] < ns <= s['receipt_ns'] + 30_000_000_000):
                                continue
                            for horizon in (5000, 30000):
                                if ns > s['receipt_ns'] + horizon * 1_000_000:
                                    continue
                                for side in ('all', 'maker'):
                                    if side == 'maker' and trade['side'] != q['side']:
                                        continue
                                    for gate in ('recv', 'recv_and_event'):
                                        cell = s['counts'][f'{horizon}ms_{side}_{gate}']
                                        if gate == 'recv_and_event' and event_time < s['received_utc']:
                                            cell['pre_anchor_events_excluded'] += 1
                                        else:
                                            cell['trades'] += 1
                                            cell['volume'] += size
            seq += 1
    if seq != 152069 or unique_updates != 60707 or snapshots != 200:
        fail('Pinned raw source census mismatch')
    if (report_a['source_messages_audited'] != seq
            or report_b['source_messages_replayed'] != seq
            or (source / 'manifest.json').read_bytes() != original_manifest):
        fail('Mutable manifest or incorrect report census')
    result_rows = []
    for id_, q in sorted(a.items(), key=lambda kv: kv[1]['signal_sequence']):
        cells = states[id_]['counts']
        # Exact published v19-A results: 30-second all-side with both receive and
        # event-time gate. Side matching was a diagnostic rather than primary fill.
        aa = cells['30000ms_all_recv_and_event']['volume']
        am = cells['30000ms_maker_recv_and_event']['volume']
        if aa != dec(q['eligible_trade_volume_any_side_exact_price']) or am != dec(q['side_matching_maker_label_volume_diagnostic_only']):
            fail('Original v19-A reported trade volume diverges: ' + id_)
        if id_ in b:
            bb = cells['5000ms_maker_recv']['volume']
            if bb != dec(b[id_]['same_exact_price_maker_side_historical_update_volume_base']):
                fail('Original v19-B reported trade volume diverges: ' + id_)
        else:
            bb = None
        result_rows.append({
            'id': id_, 'product_id': q['product_id'], 'passive_side': q['side'],
            'anchor_source_sequence': q['anchor_sequence'],
            'anchor_price_usd': q['posted_price_hypothetical'],
            'v19_b_included_under_upstream_taker_depth_gate': id_ in b,
            'v19_a_30s_all_sides_event_gate_volume': str(aa),
            'v19_a_30s_maker_side_event_gate_volume': str(am),
            'v19_b_5s_maker_side_receive_only_volume': str(bb) if bb is not None else None,
            'matrix': {k:{'volume':str(v['volume']), 'trades':v['trades'],
                           'pre_anchor_events_excluded':v['pre_anchor_events_excluded']}
                       for k,v in cells.items()},
        })
    return {
        'status': 'V19_A_B_DEFINITIONS_RECONCILED_ON_SAME_ORIGINAL',
        'source_manifest_sha256': root_hash, 'source_messages': seq,
        'unique_trade_update_ids': unique_updates, 'historical_subscription_snapshots_excluded': snapshots,
        'exact_duplicate_update_ids_excluded': repeated,
        'verified_real_maker_fill_lower_bound_base': '0',
        'actual_order_fills_observed': False, 'independent_market_truth_proven': False,
        'summary': {
            'shared_anchors_same_sequence_price_visible_qty': True,
            'v19_a': '7 anchors including 2 failing earlier taker top-level capacity; 30s; historical exact-price volume of all sides with receiver+trade-event preanchor exclusion; 10bps hypothetical fee',
            'v19_b': '5 anchors passing taker capacity; 5s; historical exact-price matching resting maker side; receiver-only time gate; 6bps hypothetical fee',
            'economic_interpretation': 'Side, horizon, event-time gate, eligibility and fee policy differ. Neither computation observes actual filled quantity or calibrated queue position.'},
        'rows': result_rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--report-a', type=Path, required=True)
    parser.add_argument('--report-b', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = reconcile(args.source, load(args.report_a.read_bytes()), load(args.report_b.read_bytes()))
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + '\n')
    print(json.dumps({'status':result['status'], 'rows':len(result['rows']),
                       'unique_update_trades':result['unique_trade_update_ids']}))


if __name__ == '__main__':
    main()
