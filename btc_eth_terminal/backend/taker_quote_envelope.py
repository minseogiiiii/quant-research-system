"""Fail-closed, read-only indicative taker quote *envelope* for archived BTC/ETH L2.

NOT a fill simulator. Historical Coinbase public L2 is not an exchange-side order
acknowledgment, point-in-time order-routing clock, depth/queue/impact oracle, or
tradeable-alpha proof. Prices are explicitly hypothetical, conditional on
user-supplied one-way taker fee, slippage haircut and a local receipt delay.

Intents are pre-supplied by immutable source sequence, never generated from
future labels. For each intent the engine takes the FIRST subsequent product
L2 book observation after the fixed local monotonic delay, with no later
cherry-picking and no partial fills. Quarantined/gapped archives are rejected.

No credentials, exchange writes, network calls, dependencies or strategy PnL.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import heapq
import json
from pathlib import Path
import re

from audit_soak import audit

PRODUCTS = ('BTC-USD', 'ETH-USD')
ID_PATTERN = re.compile(r'[A-Za-z0-9_.-]{1,64}\Z')
BPS = Decimal(10_000)
MAX_OBSERVATIONS = 250_000


class EnvelopeError(ValueError):
    pass


def unique_json(data):
    def strict_pairs(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise EnvelopeError('Ambiguous duplicate JSON key')
            out[key] = value
        return out
    def reject_constant(value):
        raise EnvelopeError('Non-finite JSON token ' + value)
    try:
        return json.loads(data, object_pairs_hook=strict_pairs, parse_constant=reject_constant)
    except (ValueError, UnicodeError, TypeError) as exc:
        raise EnvelopeError('Malformed archive/request JSON') from exc


def number(value, name, *, allow_zero=False, upper=None):
    if not isinstance(value, str) or not 1 <= len(value) <= 32:
        raise EnvelopeError(f'{name} requires bounded decimal TEXT')
    try:
        d = Decimal(value)
    except InvalidOperation as exc:
        raise EnvelopeError(f'Invalid {name} decimal') from exc
    if (not d.is_finite() or d < 0 or (d == 0 and not allow_zero)
            or d.adjusted() > 14 or d.as_tuple().exponent < -24
            or (upper is not None and d > upper)):
        raise EnvelopeError(f'{name} outside strictly bounded domain')
    return d


def integer(value, name, lo=0, hi=250_000):
    if type(value) is not int or not lo <= value <= hi:
        raise EnvelopeError(f'Invalid {name}')
    return value


@dataclass(frozen=True)
class Assumptions:
    """All economics are explicit, hypothetical and not verified venue facts."""
    latency_ms: int
    max_wait_ms: int
    max_signal_quote_age_ms: int
    one_way_taker_fee_bps: str
    extra_slippage_bps: str
    top_level_capacity_fraction: str

    def validated(self):
        integer(self.latency_ms, 'latency_ms', 1, 60_000)
        integer(self.max_wait_ms, 'max_wait_ms', self.latency_ms, 60_000)
        integer(self.max_signal_quote_age_ms, 'max_signal_quote_age_ms', 1, 5_000)
        fee = number(self.one_way_taker_fee_bps, 'fee bps', allow_zero=True, upper=Decimal(100))
        slip = number(self.extra_slippage_bps, 'slippage bps', allow_zero=True, upper=Decimal(500))
        capacity = number(self.top_level_capacity_fraction, 'depth fraction', upper=Decimal(1))
        return fee, slip, capacity


class Book:
    def __init__(self):
        self.bid = {}
        self.offer = {}
        self.bid_heap = []
        self.offer_heap = []

    def replace(self):
        self.__init__()

    def change(self, side, price, quantity):
        if side not in ('bid','offer'):
            raise EnvelopeError('Invalid book side')
        table = self.bid if side == 'bid' else self.offer
        heap = self.bid_heap if side == 'bid' else self.offer_heap
        key = -price if side == 'bid' else price
        if not quantity:
            table.pop(price, None)
        else:
            table[price] = quantity
            heapq.heappush(heap, key)
        # bounded lazy heap compaction avoids unbounded snapshot/update history
        if len(heap) > 4 * len(table) + 1024:
            heap[:] = [-v if side == 'bid' else v for v in table]
            heapq.heapify(heap)

    def best(self, side):
        table = self.bid if side == 'bid' else self.offer
        heap = self.bid_heap if side == 'bid' else self.offer_heap
        while heap:
            price = -heap[0] if side == 'bid' else heap[0]
            if price in table:
                return price, table[price]
            heapq.heappop(heap)
        return None


def _intents(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
        raise EnvelopeError('Require 1..32 explicit, preselected observation intents')
    prepared = {}
    used_ids = set()
    for row in rows:
        if (not isinstance(row, dict)
                or set(row) != {'id', 'product_id', 'side', 'quantity', 'signal_sequence'}):
            raise EnvelopeError('Unknown or missing observation intent fields')
        key = row['id']
        if not isinstance(key, str) or ID_PATTERN.fullmatch(key) is None or key in used_ids:
            raise EnvelopeError('Unsafe or duplicate observation identifier')
        used_ids.add(key)
        product = row['product_id']
        side = row['side']
        if product not in PRODUCTS or side not in ('BUY', 'SELL'):
            raise EnvelopeError('Unsupported product or side')
        seq = integer(row['signal_sequence'], 'signal_sequence', 0, MAX_OBSERVATIONS - 1)
        intent = {'id': key, 'product_id': product, 'side': side,
                  'quantity': number(row['quantity'], 'quantity', upper=Decimal(1_000_000)),
                  'signal_sequence': seq, 'state': 'AWAIT_SIGNAL'}
        prepared.setdefault(seq, []).append(intent)
    return prepared


def _outcome(i, status, **extra):
    return {'id': i['id'], 'product_id': i['product_id'], 'side': i['side'],
            'signal_sequence': i['signal_sequence'], 'requested_quantity': str(i['quantity']),
            'status': status, 'hypothetical_quote_not_fill': True, **extra}


def evaluate(source: Path, observation_intents: list, assumptions: Assumptions) -> dict:
    fee, slippage, capacity_fraction = assumptions.validated()
    pending_by_seq = _intents(observation_intents)
    expected_intents = sum(map(len, pending_by_seq.values()))
    source = Path(source)
    if source.is_symlink():
        raise EnvelopeError('Unsafe archive root')
    manifest_path = source / 'manifest.json'
    initial_manifest_bytes = manifest_path.read_bytes()
    admission = audit(source)
    if (not admission['eligible_for_point_in_time_feature_research']
            or admission['connection_count'] != 1
            or admission['message_count'] > MAX_OBSERVATIONS):
        raise EnvelopeError('Quarantined, incomplete, multi-connection or oversize source')
    if manifest_path.read_bytes() != initial_manifest_bytes:
        raise EnvelopeError('Manifest mutated during source admission')
    manifest = unique_json(initial_manifest_bytes)
    book = {p: Book() for p in PRODUCTS}
    snapshots = set()
    last_l2_ns = {p: None for p in PRODUCTS}
    waiting = []
    finished = {}
    last_ns = None
    count = 0
    latency_ns = assumptions.latency_ms * 1_000_000
    wait_ns = assumptions.max_wait_ms * 1_000_000
    stale_ns = assumptions.max_signal_quote_age_ms * 1_000_000

    for chunk in manifest['chunks']:
        file_path = source / chunk['file']
        if (file_path.is_symlink() or not file_path.is_file()
                or not file_path.resolve().is_relative_to(source.resolve())):
            raise EnvelopeError('Replaced or unsafe source chunk')
        payload = file_path.read_bytes()
        if sha256(payload).hexdigest() != chunk['archive_sha256']:
            raise EnvelopeError('Archive chunk changed after admission')
        if len(payload) != chunk['archived_line_bytes']:
            raise EnvelopeError('Archive chunk byte count changed')
        for line in payload.splitlines(keepends=True):
            if not line.endswith(b'\n'):
                raise EnvelopeError('Incomplete source row')
            row = unique_json(line)
            raw = row['raw_text']
            if not isinstance(raw, str) or sha256(raw.encode('utf-8')).hexdigest() != row['sha256']:
                raise EnvelopeError('Source wrapper changed since admission')
            msg = unique_json(raw)
            ns_text = row['local_monotonic_ns']
            if not isinstance(ns_text, str) or not ns_text.isdecimal() or len(ns_text) > 25:
                raise EnvelopeError('Invalid local monotonic clock')
            mono = int(ns_text)
            if last_ns is not None and mono < last_ns:
                raise EnvelopeError('Monotonic receipt clock regressed')
            last_ns = mono
            seq = msg['sequence_num']
            if seq != count:
                raise EnvelopeError('Source sequence changed during pricing replay')
            count += 1
            affected = set()
            if msg['channel'] == 'l2_data':
                for event in msg['events']:
                    p = event['product_id']
                    if event['type'] == 'snapshot':
                        book[p].replace()
                        snapshots.add(p)
                    for ch in event['updates']:
                        book[p].change(ch['side'], number(ch['price_level'], 'source price'),
                                       number(ch['new_quantity'], 'source size', allow_zero=True))
                    last_l2_ns[p] = mono
                    affected.add(p)
            if seq in pending_by_seq:
                for i in pending_by_seq.pop(seq):
                    p = i['product_id']
                    bid, ask = book[p].best('bid'), book[p].best('offer')
                    if (len(snapshots) != 2 or bid is None or ask is None
                            or last_l2_ns[p] is None):
                        finished[i['id']] = _outcome(i, 'REJECTED_NOT_READY_AT_SIGNAL')
                        continue
                    if mono - last_l2_ns[p] > stale_ns:
                        finished[i['id']] = _outcome(i, 'REJECTED_STALE_SIGNAL_BOOK')
                        continue
                    i['decision_mid'] = (bid[0] + ask[0]) / 2
                    i['decision_ns'] = mono
                    i['signal_received_utc'] = row['received_utc']
                    waiting.append(i)
            future_waiting = []
            for i in waiting:
                if seq <= i['signal_sequence']:
                    future_waiting.append(i)
                    continue
                delta_ns = mono - i['decision_ns']
                if delta_ns > wait_ns:
                    finished[i['id']] = _outcome(i, 'REJECTED_NO_TIMELY_PRODUCT_QUOTE')
                    continue
                if delta_ns < latency_ns or i['product_id'] not in affected:
                    future_waiting.append(i)
                    continue
                p = i['product_id']
                bid, ask = book[p].best('bid'), book[p].best('offer')
                if bid is None or ask is None or bid[0] >= ask[0]:
                    finished[i['id']] = _outcome(i, 'REJECTED_INVALID_TARGET_BOOK')
                    continue
                target_price, visible_qty = ask if i['side'] == 'BUY' else bid
                max_qty = visible_qty * capacity_fraction
                if i['quantity'] > max_qty:
                    finished[i['id']] = _outcome(i, 'REJECTED_INSUFFICIENT_BEST_LEVEL_DEPTH',
                         observed_best_level_qty=str(visible_qty), max_assumed_qty=str(max_qty),
                         quote_sequence=seq)
                    continue
                mid = (bid[0] + ask[0]) / 2
                spread_bps = (ask[0] - bid[0]) / mid * BPS
                half_spread_bps = ((target_price - mid) if i['side'] == 'BUY'
                                   else (mid - target_price)) / mid * BPS
                hypothetical_price = (target_price * (1 + slippage / BPS)
                                      if i['side'] == 'BUY' else target_price * (1 - slippage / BPS))
                gross = hypothetical_price * i['quantity']
                hypothetical_fee = gross * fee / BPS
                cash_flow = (-gross-hypothetical_fee if i['side'] == 'BUY'
                             else gross-hypothetical_fee)
                # Do NOT label quote drift or a modeled cash flow as a fill or PnL.
                finished[i['id']] = _outcome(i, 'INDICATIVE_TAKER_QUOTE_ONLY',
                    quote_sequence=seq,
                    quote_received_utc=row['received_utc'],
                    local_monotonic_wait_ms=str(Decimal(delta_ns)/Decimal(1_000_000)),
                    signal_received_utc=i['signal_received_utc'],
                    signal_mid=str(i['decision_mid']), target_mid=str(mid),
                    market_best_bid=str(bid[0]), market_best_ask=str(ask[0]),
                    observed_best_level_qty=str(visible_qty), max_assumed_qty=str(max_qty),
                    full_spread_bps=str(spread_bps),half_spread_bps=str(half_spread_bps),
                    price_drift_from_signal_mid_bps=str((mid-i['decision_mid'])/i['decision_mid']*BPS),
                    hypothetical_extra_slippage_bps=str(slippage),
                    hypothetical_quoted_price=str(hypothetical_price),
                    hypothetical_gross_notional_usd=str(gross),
                    hypothetical_one_way_fee_usd=str(hypothetical_fee),
                    hypothetical_cashflow_usd=str(cash_flow),
                    no_order_sent=True)
            waiting = future_waiting
    for seq, intents in pending_by_seq.items():
        for i in intents:
            finished[i['id']] = _outcome(i, 'REJECTED_SIGNAL_SEQUENCE_NOT_IN_SOURCE')
    for i in waiting:
        finished[i['id']] = _outcome(i, 'REJECTED_NO_TIMELY_PRODUCT_QUOTE')
    if count != admission['message_count']:
        raise EnvelopeError('Source tally changed since admission')
    if len(finished) != expected_intents:
        raise EnvelopeError('Intent accounting mismatch')
    ordered = sorted(finished.values(), key=lambda x: (x['signal_sequence'],x['id']))
    return {'status':'HYPOTHETICAL_TAKER_QUOTES_NOT_EXECUTIONS',
            'market':'Coinbase public spot BTC-USD ETH-USD',
            'source_manifest_sha256':sha256(initial_manifest_bytes).hexdigest(),
            'source_messages_audited':count,'connection_count':admission['connection_count'],
            'receipt_clock':'NODE_LOCAL_MONOTONIC_WITHIN_ONE_ARCHIVED_PROCESS',
            'exchange_order_route_clock_calibrated':False,
            'one_way_fee_bps_explicit_unverified':str(fee),
            'extra_slippage_bps_explicit_unverified':str(slippage),
            'latency_ms_assumed_not_measured':assumptions.latency_ms,
            'max_wait_ms':assumptions.max_wait_ms,
            'top_level_capacity_fraction':str(capacity_fraction),
            'results':ordered,
            'fills_verified':False,'maker_queue_verified':False,'market_impact_verified':False,
            'historic_venue_fee_tier_verified':False,'strategy_alpha_verified':False,
            'trading_or_api_writes_enabled':False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source', type=Path)
    p.add_argument('--intents', type=Path, required=True,
                   help='JSON list of observations {id,product_id,side,quantity,signal_sequence}')
    p.add_argument('--latency-ms', type=int, required=True)
    p.add_argument('--max-wait-ms', type=int, required=True)
    p.add_argument('--max-signal-quote-age-ms', type=int, required=True)
    p.add_argument('--taker-fee-bps', type=str, required=True)
    p.add_argument('--extra-slippage-bps', type=str, required=True)
    p.add_argument('--top-level-capacity-fraction', type=str, required=True)
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    intents = unique_json(args.intents.read_bytes())
    values = Assumptions(args.latency_ms,args.max_wait_ms,args.max_signal_quote_age_ms,
                         args.taker_fee_bps,args.extra_slippage_bps,args.top_level_capacity_fraction)
    result = evaluate(args.source,intents,values)
    output = json.dumps(result,indent=2,sort_keys=True)+'\n'
    if args.output:args.output.write_text(output)
    print(output)


if __name__ == '__main__':
    main()
