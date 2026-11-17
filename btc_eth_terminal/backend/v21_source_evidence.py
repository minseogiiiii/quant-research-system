"""Read-only, source-backed Coinbase BTC/ETH L2 as-of quote snapshots.

This is a *new, separate* receiver-time diagnostic cohort, NOT the user's
unrecovered original feature/label data, independent venue truth, or a signal.
Actual archived source is independently admitted by audit_soak before use.
No future labels, fill simulation, orders, model training or execution output.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

from audit_soak import audit as admit_source

PRODUCTS = ('BTC-USD', 'ETH-USD')
STEP_MS = 300_000


def iso_ms(value: str) -> int:
    if not isinstance(value, str) or not value.endswith('Z'):
        raise ValueError('Explicit UTC timestamp required')
    dt = datetime.fromisoformat(value[:-1] + '+00:00')
    return int(dt.timestamp() * 1000)


def asof_rows(events, decisions_ms, max_age_ms=5000):
    """Causally apply stream in increasing receipt order, snapshot-before-delta.

    Events are (received_ms, sequence, product, kind, updates) tuples.
    Every decision takes a state snapshot before events with later receipt time.
    Any out-of-order or repeated receiver receipt/sequence invalidates input.
    """
    if not decisions_ms or sorted(set(decisions_ms)) != list(decisions_ms):
        raise ValueError('Decisions must be unique and strictly increasing')
    if any(type(x) is not int or x < 1_500_000_000_000 for x in decisions_ms):
        raise ValueError('Invalid decision time')
    books = {p: {'bid': {}, 'offer': {}} for p in PRODUCTS}
    fresh_snapshot = {p: False for p in PRODUCTS}
    last_l2 = {p: None for p in PRODUCTS}
    last_seq_by_product = {p: None for p in PRODUCTS}
    last_received = last_sequence = None
    times = iter(decisions_ms)
    current = next(times, None)
    out = []

    def snapshot(decision):
        for product in PRODUCTS:
            b = books[product]
            current_bid = max(b['bid']) if b['bid'] else None
            current_ask = min(b['offer']) if b['offer'] else None
            t = last_l2[product]
            age = None if t is None else decision-t
            if age is not None and age < 0:
                raise ValueError('Future quote leaked into a prior decision')
            if current_bid is not None and current_ask is not None and current_bid >= current_ask:
                raise ValueError('Crossed or locked book at decision time')
            good = (fresh_snapshot[product] and current_bid is not None and
                    current_ask is not None and current_bid < current_ask and
                    age is not None and age <= max_age_ms)
            record = {
                'instrument': product, 'decision_receive_utc_ms': decision,
                'status': 'RECEIVER_ASOF_L2_QUOTE' if good else 'NO_ADMISSIBLE_FRESH_QUOTE',
                'last_l2_received_utc_ms': t, 'source_message_sequence': last_seq_by_product[product],
                'quote_age_ms': age, 'clock_calibration_verified': False,
                'independent_venue_reference_verified': False,
                'original_user_features_verified': False,
                'possible_real_order_fill_verified': False,
            }
            if good:
                record.update({'best_bid':str(current_bid),'best_ask':str(current_ask),
                               'spread':str(current_ask-current_bid),
                               'displayed_best_bid_qty':str(b['bid'][current_bid]),
                               'displayed_best_ask_qty':str(b['offer'][current_ask])})
            out.append(record)

    for received, seq, product, kind, changes in events:
        if (type(received) is not int or type(seq) is not int or
                (last_received is not None and received < last_received) or
                (last_sequence is not None and (seq < last_sequence or
                   (seq == last_sequence and received != last_received)))):
            raise ValueError('Source event ordering changed or duplicate sequence')
        if product not in PRODUCTS or kind not in ('snapshot','update'):
            raise ValueError('Invalid instrument/event type')
        while current is not None and current < received:
            snapshot(current)
            current = next(times, None)
        if kind == 'snapshot':
            if fresh_snapshot[product]:
                raise ValueError('Duplicated snapshot within source connection')
            books[product] = {'bid':{},'offer':{}}
            fresh_snapshot[product]=True
        elif not fresh_snapshot[product]:
            raise ValueError('Update preceding initial snapshot')
        for update in changes:
            side = update['side']
            if side not in ('bid','offer'):
                raise ValueError('Invalid side')
            price = Decimal(update['price_level'])
            qty = Decimal(update['new_quantity'])
            if not all(x.is_finite() for x in (price,qty)) or price<=0 or qty<0:
                raise ValueError('Invalid quote number')
            if qty==0:
                books[product][side].pop(price,None)
            else:
                books[product][side][price]=qty
        last_l2[product] = received
        last_seq_by_product[product] = seq
        last_received = received
        last_sequence = seq
        # The complete archive's audit_soak admission checks all intermediate
        # updates for crossed books; quote extraction checks decision snapshots.
    while current is not None:
        snapshot(current)
        current = next(times, None)
    return out


def original_l2_events(root, chunks):
    seq_prev = None
    for chunk in chunks:
        with (root/chunk['file']).open('rb') as f:
            for line in f:
                row=json.loads(line)
                msg=json.loads(row['raw_text'])
                seq=msg['sequence_num']
                if seq_prev is not None and seq != seq_prev+1:
                    raise ValueError('Connection-local source sequence gap')
                seq_prev=seq
                if msg['channel'] != 'l2_data':
                    continue
                stamp=iso_ms(row['received_utc'])
                for e in msg['events']:
                    yield stamp,seq,e['product_id'],e['type'],e['updates']


def create_report(root: Path, max_age_ms=5000):
    root=Path(root)
    main=admit_source(root)
    if main['status'] != 'INTERNALLY_REPLAYED_NO_KNOWN_GAP' or main['connection_count']!=1:
        raise ValueError('Source not fully admitted for receiver-as-of diagnostics')
    manifest_bytes=(root/'manifest.json').read_bytes()
    m=json.loads(manifest_bytes)
    start=iso_ms(m['sampled_from_utc'])
    end=iso_ms(m['finished_at_utc'])
    first=((start+STEP_MS-1)//STEP_MS)*STEP_MS
    # Explicit fixed 5-minute UTC decision grid. No hypothesis or signal selected.
    decisions=list(range(first,end+1,STEP_MS))
    if len(decisions)>1000:
        raise ValueError('Too many diagnostic decisions')
    rows=asof_rows(original_l2_events(root,m['chunks']),decisions,max_age_ms)
    if (root/'manifest.json').read_bytes()!=manifest_bytes:
        raise ValueError('Original source manifest mutated during PIT diagnostic')
    return {'status':'RECEIVER_ASOF_QUOTE_DIAGNOSTIC_NOT_ORIGINAL_FEATURE_PAIR',
        'source_manifest_sha256':sha256(manifest_bytes).hexdigest(),
        'source_message_count_independently_admitted':main['message_count'],
        'source_books_final_sha256':{p:main['books_final'][p]['sha256'] for p in PRODUCTS},
        'decision_grid':'UTC 5min aligned receiver-wall time',
        'max_staleness_ms':max_age_ms,'original_user_feature_label_pair_recovered':False,
        'historical_exchange_time_or_clock_calibration_verified':False,
        'eligible_for_strategy_oos_or_fills':False,
        'rows':rows}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=create_report(args.source)
    args.output.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    print(result['status'],'rows',len(result['rows']))
if __name__=='__main__': main()