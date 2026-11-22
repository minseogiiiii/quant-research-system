"""v19: read-only, conditionally bounded BTC/ETH maker *trade-flow* diagnostics.

There are no real orders or identified queue ranks. The public Coinbase L2 feed
contains *aggregated* price levels, and public trades cannot distinguish orders
canceled in front from executions or identify our hypothetical fill. All computed
quantities are conditional counterfactual scenarios, never verified fills or
rigorous upper/lower bounds on real fill outcomes. In particular the only
unconditional lower evidentiary bound is zero observed user fills.

No live network requests, credentials, exchange writes, or strategy selection.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

from post_quote_markout import IndexedBook
from taker_quote_envelope import Assumptions, EnvelopeError, evaluate, integer, number, unique_json


class MakerQueueError(ValueError):
    pass


@dataclass(frozen=True)
class MakerPolicy:
    resting_window_ms: int
    assumed_maker_fee_bps: str

    def validated(self):
        integer(self.resting_window_ms, 'resting_window_ms', 1000, 180_000)
        return number(self.assumed_maker_fee_bps, 'hypothetical maker fee', allow_zero=True,
                      upper=Decimal('100'))


def _utc(value):
    if not isinstance(value, str) or not value.endswith('Z') or len(value) > 40:
        raise MakerQueueError('Missing or malformed UTC timestamp')
    try:
        dt = datetime.fromisoformat(value[:-1] + '+00:00')
        if dt.utcoffset() != timezone.utc.utcoffset(dt):
            raise ValueError('invalid timezone')
        return dt
    except ValueError as exc:
        raise MakerQueueError('Invalid UTC timestamp') from exc


def _candidate(q, book, anchor_sequence, anchor_ns, anchor_utc):
    b = book[q['product_id']]
    bid = b.best('bid')
    offer = b.best('offer')
    if bid is None or offer is None or bid[0] <= 0 or bid[0] >= offer[0]:
        raise MakerQueueError('No valid noncrossed book at maker anchor')
    chosen = bid if q['side'] == 'BUY' else offer
    return {
        'id': q['id'], 'product_id': q['product_id'], 'side': q['side'],
        'signal_sequence': q['signal_sequence'],
        'anchor_sequence': anchor_sequence, 'anchor_monotonic_ns': anchor_ns,
        'anchor_received_utc': anchor_utc, '_anchor_dt': _utc(anchor_utc),
        'posted_price_hypothetical': str(chosen[0]),
        'price': chosen[0], 'qty': Decimal(q['requested_quantity']),
        'initial_visible_same_side_quantity': str(chosen[1]),
        'initial_ahead_qty_assumption': chosen[1],
        'eligible_trade_volume_any_side_exact_price': Decimal(0),
        'eligible_trade_count_any_side_exact_price': 0,
        'side_matching_maker_label_volume': Decimal(0),
        'excluded_pre_anchor_exchange_time_trades': 0,
        'same_price_depth_declines_observed': 0,
        'same_price_depth_decline_quantity': Decimal(0),
        'first_trade_sequence': None,
    }


def inspect(source: Path, intents: list, assumptions: Assumptions, policy: MakerPolicy) -> dict:
    maker_fee = policy.validated()
    # Upstream admission parses all source packets with a separate fully validated
    # L2 replay, and chooses the FIRST eligible post-delay L2 observation.
    baseline = evaluate(source, intents, assumptions)
    root = Path(source)
    manifest_path = root / 'manifest.json'
    if root.is_symlink() or manifest_path.is_symlink():
        raise MakerQueueError('Unsafe source root/manifest')
    manifest_bytes = manifest_path.read_bytes()
    if sha256(manifest_bytes).hexdigest() != baseline['source_manifest_sha256']:
        raise MakerQueueError('Manifest changed since baseline admission')
    manifest = unique_json(manifest_bytes)
    anchor_by_seq = {}
    for q in baseline['results']:
        if q['status'] in ('INDICATIVE_TAKER_QUOTE_ONLY', 'REJECTED_INSUFFICIENT_BEST_LEVEL_DEPTH'):
            if type(q.get('quote_sequence')) is not int:
                raise MakerQueueError('Missing bounded quote sequence')
            anchor_by_seq.setdefault(q['quote_sequence'], []).append(q)
    books = {'BTC-USD': IndexedBook(), 'ETH-USD': IndexedBook()}
    active = []
    candidates = {}
    seen_trade_ids = {}
    last_ns = None
    sequence = 0
    total_trade_updates_seen = 0
    duplicate_update_ids_skipped = 0
    trade_snapshots_excluded = 0
    for chunk in manifest['chunks']:
        file_path = root / chunk['file']
        if (file_path.is_symlink() or not file_path.is_file()
                or not file_path.resolve().is_relative_to(root.resolve())):
            raise MakerQueueError('Unsafe/missing original chunk')
        payload = file_path.read_bytes()
        if (len(payload) != chunk['archived_line_bytes']
                or sha256(payload).hexdigest() != chunk['archive_sha256']):
            raise MakerQueueError('Original chunk changed after admission')
        for line in payload.splitlines(keepends=True):
            if not line.endswith(b'\n'):
                raise MakerQueueError('Truncated source row')
            row = unique_json(line)
            raw = row['raw_text']
            if not isinstance(raw, str) or sha256(raw.encode('utf-8')).hexdigest() != row['sha256']:
                raise MakerQueueError('Changed original WebSocket payload')
            msg = unique_json(raw)
            if type(msg['sequence_num']) is not int or msg['sequence_num'] != sequence:
                raise MakerQueueError('Nonconsecutive original sequence')
            clock = row['local_monotonic_ns']
            if (not isinstance(clock, str) or not clock.isdecimal() or len(clock) > 25):
                raise MakerQueueError('Invalid receiver monotonic timestamp')
            ns = int(clock)
            if last_ns is not None and ns < last_ns:
                raise MakerQueueError('Source receiver monotonic clock regressed')
            last_ns = ns
            sequence += 1
            if msg['channel'] == 'l2_data':
                for event in msg['events']:
                    product = event['product_id']
                    if product not in books:
                        raise MakerQueueError('Unknown product in L2')
                    book = books[product]
                    if event['type'] == 'snapshot':
                        book.replace()
                    for change in event['updates']:
                        side = change['side']
                        price = number(change['price_level'], 'price')
                        quantity = number(change['new_quantity'], 'quantity', allow_zero=True)
                        for item in active:
                            if (item['product_id'] == product and
                                    (('bid' if item['side'] == 'BUY' else 'offer') == side) and
                                    price == item['price'] and
                                    item['anchor_monotonic_ns'] < ns <=
                                    item['anchor_monotonic_ns'] + policy.resting_window_ms * 1_000_000):
                                old = (book.bid if side == 'bid' else book.offer).get(price, Decimal(0))
                                if old > quantity:
                                    item['same_price_depth_declines_observed'] += 1
                                    item['same_price_depth_decline_quantity'] += old - quantity
                        book.change(side, price, quantity)
            if sequence - 1 in anchor_by_seq:
                for q in anchor_by_seq[sequence - 1]:
                    c = _candidate(q, books, sequence - 1, ns, row['received_utc'])
                    if c['id'] in candidates:
                        raise MakerQueueError('Duplicate maker candidate identifier')
                    candidates[c['id']] = c
                    active.append(c)
            if msg['channel'] == 'market_trades':
                for event in msg['events']:
                    event_type = event['type']
                    if event_type == 'snapshot':
                        trade_snapshots_excluded += len(event['trades'])
                        continue
                    if event_type != 'update':
                        raise MakerQueueError('Unexpected market_trades event type')
                    for trade in event['trades']:
                        p = trade['product_id']
                        if p not in books or trade['side'] not in ('BUY', 'SELL'):
                            raise MakerQueueError('Unexpected market trade product or side')
                        ident = trade['trade_id']
                        if not isinstance(ident, str) or not ident or len(ident) > 128:
                            raise MakerQueueError('Malformed trade ID')
                        trade_id = (p, ident)
                        identity = (trade['price'], trade['size'], trade['time'], trade['side'])
                        if trade_id in seen_trade_ids:
                            if seen_trade_ids[trade_id] != identity:
                                raise MakerQueueError('Conflicting duplicate market trade ID')
                            duplicate_update_ids_skipped += 1
                            continue
                        seen_trade_ids[trade_id] = identity
                        total_trade_updates_seen += 1
                        price = number(trade['price'], 'trade price')
                        size = number(trade['size'], 'trade size')
                        trade_time = _utc(trade['time'])
                        for c in active:
                            if (p != c['product_id'] or price != c['price'] or
                                    ns <= c['anchor_monotonic_ns'] or
                                    ns > c['anchor_monotonic_ns'] + policy.resting_window_ms * 1_000_000):
                                continue
                            # Both receive-sequence and event-time gates. Clock skew is
                            # NOT calibrated, so these remain diagnostic exclusions.
                            if trade_time < c['_anchor_dt']:
                                c['excluded_pre_anchor_exchange_time_trades'] += 1
                                continue
                            c['eligible_trade_volume_any_side_exact_price'] += size
                            c['eligible_trade_count_any_side_exact_price'] += 1
                            if trade['side'] == c['side']:
                                c['side_matching_maker_label_volume'] += size
                            if c['first_trade_sequence'] is None:
                                c['first_trade_sequence'] = sequence - 1
            active = [c for c in active if ns < c['anchor_monotonic_ns'] +
                      policy.resting_window_ms * 1_000_000]
    if sequence != baseline['source_messages_audited']:
        raise MakerQueueError('Original message tally changed')
    if manifest_path.read_bytes() != manifest_bytes:
        raise MakerQueueError('Original manifest changed during replay')
    if len(candidates) != sum(len(x) for x in anchor_by_seq.values()):
        raise MakerQueueError('Lost a valid anchor')
    outcome = []
    for q in baseline['results']:
        c = candidates.get(q['id'])
        if c is None:
            outcome.append({'id': q['id'], 'product_id': q['product_id'], 'side': q['side'],
                            'status': 'NO_ADMITTED_MAKER_ANCHOR',
                            'upstream_quote_status': q['status'],
                            'observed_user_fill_lower_bound': '0'})
            continue
        flow = c['eligible_trade_volume_any_side_exact_price']
        ahead = c['initial_ahead_qty_assumption']
        qty = c['qty']
        complete = last_ns >= c['anchor_monotonic_ns'] + policy.resting_window_ms * 1_000_000
        # The two counterfactual allocations assume all qualifying exact-price
        # trades represent eligible opposite-side demand. Neither is a verified
        # real-world upper bound or a probability model; canceled ahead volume is
        # unknown, so FIFO no-cancel can be more pessimistic than actual fills.
        result = {
            'id': c['id'], 'product_id': c['product_id'], 'side': c['side'],
            'signal_sequence': c['signal_sequence'], 'anchor_sequence': c['anchor_sequence'],
            'anchor_received_utc': c['anchor_received_utc'],
            'posted_price_hypothetical': c['posted_price_hypothetical'],
            'requested_quantity': str(qty),
            'initial_visible_same_side_quantity': c['initial_visible_same_side_quantity'],
            'resting_window_ms': policy.resting_window_ms,
            'status': 'CONDITIONAL_FLOW_DIAGNOSTIC_COMPLETE' if complete else 'INCOMPLETE_RESTING_WINDOW',
            'eligible_trade_count_any_side_exact_price': c['eligible_trade_count_any_side_exact_price'],
            'eligible_trade_volume_any_side_exact_price': str(flow),
            'side_matching_maker_label_volume_diagnostic_only': str(c['side_matching_maker_label_volume']),
            'pre_anchor_exchange_time_trade_exclusions': c['excluded_pre_anchor_exchange_time_trades'],
            'same_price_l2_depth_declines_not_proof_of_fills': c['same_price_depth_declines_observed'],
            'same_price_l2_depth_decline_quantity_not_proof_of_fills': str(c['same_price_depth_decline_quantity']),
            'first_qualifying_trade_sequence': c['first_trade_sequence'],
            'observed_user_fill_lower_bound': '0',
            'hypothetical_no_ahead_queue_quantity': str(min(qty, flow)) if complete else None,
            'hypothetical_fifo_no_ahead_cancellations_quantity': str(min(qty, max(Decimal(0), flow-ahead))) if complete else None,
            'hypothetical_no_ahead_queue_fill_fraction': str(min(qty, flow)/qty) if complete else None,
            'hypothetical_fifo_no_ahead_cancellations_fill_fraction': str(min(qty, max(Decimal(0), flow-ahead))/qty) if complete else None,
            'verified_actual_fill_quantity': None,
            'upstream_quote_status': q['status'],
        }
        outcome.append(result)
    return {
        'status': 'MAKER_PUBLIC_TRADE_FLOW_SCENARIOS_NOT_FILLS',
        'source_manifest_sha256': baseline['source_manifest_sha256'],
        'source_messages_audited': sequence, 'connection_count': baseline['connection_count'],
        'resting_window_ms_predeclared': policy.resting_window_ms,
        'latency_ms_receiver_clock_assumption': assumptions.latency_ms,
        'maker_fee_bps_assumed_unverified': str(maker_fee),
        'snapshot_trades_excluded': trade_snapshots_excluded,
        'unique_update_trades_observed': total_trade_updates_seen,
        'duplicate_update_trade_ids_excluded': duplicate_update_ids_skipped,
        'side_semantics_not_required_for_primary_all_side_trade_volume': True,
        'queue_rank_known': False, 'cancellations_separable_from_l2': False,
        'order_acknowledgements_measured': False, 'clock_synchronization_calibrated': False,
        'market_impact_measured': False, 'real_orders_submitted': False,
        'actual_fills_verified': False, 'actual_fill_probability_estimated': False,
        'realized_pnl_computed': False, 'strategy_alpha_verified': False,
        'historical_blind_oos_used': False, 'scope': 'BTC_ETH_COINBASE_SPOT_RESEARCH_ONLY',
        'trade_volume_warning': 'Counterfactual order can change market behavior. These scenario allocations are NOT guaranteed attainable fills, actual-fill upper bounds, or independently observed queue positions.',
        'results': outcome,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--intents', type=Path, required=True)
    parser.add_argument('--resting-window-ms', type=int, required=True)
    parser.add_argument('--maker-fee-bps', required=True)
    parser.add_argument('--latency-ms', type=int, default=250)
    parser.add_argument('--max-wait-ms', type=int, default=5000)
    parser.add_argument('--max-signal-quote-age-ms', type=int, default=2500)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = inspect(args.source, unique_json(args.intents.read_bytes()),
        Assumptions(args.latency_ms, args.max_wait_ms, args.max_signal_quote_age_ms,
                    '10', '5', '0.25'),
        MakerPolicy(args.resting_window_ms, args.maker_fee_bps))
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps({'status':result['status'], 'source_messages':result['source_messages_audited'],
        'intents':len(result['results']), 'output':str(args.output)}))


if __name__ == '__main__':
    main()
