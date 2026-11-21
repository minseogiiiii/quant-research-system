"""Strict two-connection source integrity and conservative public REST book corroboration.

A locally imposed WebSocket close is a controlled recovery experiment, not an
exchange outage. REST responses and WS messages have different as-of semantics:
any top-of-book overlap is only corroboration, not an atomic comparison.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from audit_l2_deep import (
    PRODUCTS, StrictL2Error, _json, _number, _time, deep_replay,
)
from audit_l2_pilot import audit_session
from l2_recovery_gate import BookAdmissionGate

EXPECTED = ('segment_before_disconnect', 'segment_after_reconnect')
ALLOWED_PREFIX = 'https://api.coinbase.com/api/v3/brokerage/market/product_book?'


def _read_regular(path: Path, base: Path, max_size: int) -> bytes:
    if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(base.resolve()):
        raise StrictL2Error('Missing or redirected evidence file')
    if path.stat().st_size > max_size or path.stat().st_size == 0:
        raise StrictL2Error('Evidence file outside permitted bounds')
    return path.read_bytes()


def _rest_meta(segment: Path, product: str, window_start, window_end) -> dict:
    stem = product.replace('-', '_')
    dirpath = segment / 'reference_books'
    meta = _json(_read_regular(dirpath / (stem + '.meta.json'), segment, 8000))
    if meta.get('product') != product or not isinstance(meta.get('url'), str):
        raise StrictL2Error('REST product or URL missing')
    if (not meta['url'].startswith(ALLOWED_PREFIX)
            or 'product_id=' + product not in meta['url']
            or 'limit=5' not in meta['url']):
        raise StrictL2Error('Wrong REST source URL')
    if meta.get('success') is not True or meta.get('status_code') != 200:
        return {'product': product, 'availability': 'REQUEST_FAILED',
                'failure': meta.get('error', 'HTTP ' + str(meta.get('status_code')))}
    start = _time(meta.get('requested_at_utc'))
    end = _time(meta.get('received_at_utc'))
    if not (window_start <= start <= end <= window_end + timedelta(seconds=15)):
        raise StrictL2Error('REST source request timing inconsistent with WS capture')
    rawfile = meta.get('raw_file')
    if rawfile != 'reference_books/' + stem + '.json':
        raise StrictL2Error('Unexpected REST raw archive path')
    raw = _read_regular(segment / rawfile, segment, 1_000_000)
    if len(raw) != meta.get('raw_bytes') or hashlib.sha256(raw).hexdigest() != meta.get('raw_sha256'):
        raise StrictL2Error('REST raw response digest disagrees with metadata')
    book = _json(raw)
    book = book.get('pricebook') if isinstance(book, dict) else None
    if not isinstance(book, dict) or book.get('product_id') != product:
        raise StrictL2Error('REST pricebook is missing or mislabeled')
    vals = {}
    for side, k in [('bid', 'bids'), ('offer', 'asks')]:
        levels = book.get(k)
        if not isinstance(levels, list) or not levels or len(levels) > 5:
            raise StrictL2Error('REST returned unexpected number of price levels')
        values = []
        for e in levels:
            if not isinstance(e, dict):
                raise StrictL2Error('Malformed REST level')
            values.append((_number(e.get('price')), _number(e.get('size'))))
        vals[side] = (max if side == 'bid' else min)(p for p, q in values)
    if vals['bid'] >= vals['offer']:
        raise StrictL2Error('REST reference crossed or locked')
    book_time = _time(book.get('time'))
    if abs((book_time - end).total_seconds()) > 60:
        raise StrictL2Error('REST reference exchange timestamp implausible')
    return {'product':product, 'availability':'ARCHIVED_SOURCE_VALID',
            'raw_sha256': meta['raw_sha256'], 'raw_bytes':len(raw),
            'request_utc':start.isoformat(), 'response_utc':end.isoformat(),
            'source_book_time_utc':book_time.isoformat(),
            'best_bid_rest':str(vals['bid']), 'best_ask_rest':str(vals['offer']),
            'rest_best_prices_venue_authenticated':False,
            'synchrony_with_ws': 'NOT_ATOMIC',
            'requested_at':start, 'received_at':end,
            'bid_decimal':vals['bid'],'ask_decimal':vals['offer']}


def _stream_quote_history(segment: Path, max_size=40_000_000):
    """Price-state series for request/response bracketing, not latency inference."""
    raw = segment / 'raw_websocket_payloads.jsonl'
    if not raw.is_file() or raw.is_symlink() or raw.stat().st_size > max_size:
        raise StrictL2Error('Invalid WS archive')
    books = {p: {'bid':{}, 'offer':{}} for p in PRODUCTS}
    series = {p:[] for p in PRODUCTS}
    with raw.open('rb') as stream:
        for line in stream:
            row = _json(line)
            received = _time(row.get('received_utc'))
            event = _json(row.get('raw_text'))
            if event.get('channel') != 'l2_data':
                continue
            for e in event.get('events', []):
                product = e['product_id']
                if product not in PRODUCTS:
                    raise StrictL2Error('Wrong L2 product')
                if e['type'] == 'snapshot':
                    books[product] = {'bid':{}, 'offer':{}}
                for change in e.get('updates', []):
                    side = books[product][change['side']]
                    p = _number(change['price_level'])
                    q = _number(change['new_quantity'], allow_zero=True)
                    if q:
                        side[p] = q
                    else:
                        side.pop(p, None)
                b = books[product]['bid']; a = books[product]['offer']
                if b and a:
                    series[product].append((received, max(b), min(a)))
    return series


def _window_comparison(points, ref):
    if ref['availability'] != 'ARCHIVED_SOURCE_VALID':
        return {'status':'REST_NOT_AVAILABLE'}
    begin = ref['requested_at'] - timedelta(seconds=1)
    finish = ref['received_at'] + timedelta(seconds=1)
    moments = [t for t, _, _ in points]
    lo = max(0, bisect_left(moments, begin) - 1)
    hi = bisect_right(moments, finish)
    sample = points[lo:hi]
    if not sample:
        return {'status':'NO_WS_BOOK_DURING_HTTP_BRACKET'}
    bids = [p[1] for p in sample]
    asks = [p[2] for p in sample]
    return {'status':'REST_VS_WS_ASYNCHRONOUS_TIME_WINDOW',
            'observed_ws_books_in_window': len(sample),
            'rest_bid_in_observed_ws_range':min(bids) <= ref['bid_decimal'] <= max(bids),
            'rest_ask_in_observed_ws_range':min(asks) <= ref['ask_decimal'] <= max(asks),
            'ws_bid_range':[str(min(bids)), str(max(bids))],
            'ws_ask_range':[str(min(asks)), str(max(asks))],
            'atomic_book_equality_established':False,
            'timestamp_clock_synchronization_established':False}



def _gate_session(segment: Path, gate: BookAdmissionGate) -> dict:
    """Reconsume real messages with a strict close/reconnect-only admission gate."""
    gate.connected()
    first_ready = None
    counter = 0
    with (segment / 'raw_websocket_payloads.jsonl').open('rb') as source:
        for row in source:
            counter += 1
            event = _json(_json(row).get('raw_text'))
            admitted = gate.consume(event)
            if admitted and first_ready is None:
                first_ready = counter
    if not gate.ready or not all(gate.updates[p] > 0 for p in PRODUCTS):
        raise StrictL2Error('Never established two-product ready state after restart')
    result = {'messages_examined': counter,
              'messages_until_both_fresh_snapshots': first_ready,
              'state_at_end_of_session':gate.state,
              'updates_after_snapshot':dict(gate.updates)}
    gate.disconnected()
    if gate.ready or gate.snapshots:
        raise StrictL2Error('Old connection retained a market-data-ready state')
    result['state_after_disconnect'] = gate.state
    return result


def audit_reconnect(folder: Path) -> dict:
    folder = Path(folder).resolve()
    parent = _json(_read_regular(folder/'reconnect_summary.json',folder,16000))
    if (parent.get('scope')!='PUBLIC_BTC_ETH_NO_AUTH_NO_ORDERS'
            or parent.get('intentional_reconnection') is not True
            or parent.get('status')!='TWO_SEGMENTS_CAPTURED_NOT_AUDITED'
            or parent.get('executable_alpha_verified') is not False):
        raise StrictL2Error('Unverified reconnect source manifest')
    declared=parent.get('segments')
    if not isinstance(declared,list) or [x.get('label') for x in declared]!=list(EXPECTED):
        raise StrictL2Error('Missing ordered source segments')
    results=[]; previous_end=None; gate=BookAdmissionGate()
    for name, entry in zip(EXPECTED, declared):
        if entry.get('folder') != 'reconnect_evidence/'+name:
            raise StrictL2Error('Changed segment path in parent')
        segment=folder/name
        digest=hashlib.sha256(_read_regular(segment/'capture_status.json',folder,8000)).hexdigest()
        if digest != entry.get('status_sha256'):
            raise StrictL2Error('Source manifest status file hash mismatch')
        captured = _json((segment/'capture_status.json').read_bytes())
        if captured.get('controlled_disconnect_requested') is not True:
            raise StrictL2Error('Source does not record controlled session close')
        if captured.get('finish_reason')!='intentional_rotation':
            raise StrictL2Error('Missing intentional rotation evidence')
        begin=_time(captured.get('start_utc'));end=_time(captured.get('end_utc'))
        if previous_end is not None and begin <= previous_end:
            raise StrictL2Error('Reconnect segments overlap or out of order')
        previous_end=end
        replay=deep_replay(segment)
        primary=audit_session(segment)
        if replay['input_stream_sha256']!=primary['source_text_stream_sha256']:
            raise StrictL2Error('Independent replay source digest differs')
        if replay['connection_sequence_first'] != 0:
            raise StrictL2Error('Cannot establish fresh connection sequence origin')
        for product in PRODUCTS:
            p=replay['per_product'][product];v=primary['per_product'][product]
            if p['snapshot_count']!=1 or p['best_bid_final']!=v['best_bid_final']:
                raise StrictL2Error('Independent replay mismatch')
            if p['best_ask_final']!=v['best_ask_final']:
                raise StrictL2Error('Independent replay mismatch')
            if replay['collector_telemetry'][product]['capture_telemetry_mismatch']:
                raise StrictL2Error('Producer source count mismatch')
        gate_result=_gate_session(segment, gate)
        prices=_stream_quote_history(segment)
        refs=[]
        for p in PRODUCTS:
            r=_rest_meta(segment,p,begin,end)
            comp=_window_comparison(prices[p],r)
            refs.append({k:v for k,v in r.items() if k not in (
                'requested_at','received_at','bid_decimal','ask_decimal')} | {'comparison':comp})
        results.append({'segment':name,'records':replay['total_messages'],
            'first_utc':begin.isoformat(),'last_utc':end.isoformat(),
            'connection_seq_first':replay['connection_sequence_first'],
            'connection_seq_last':replay['connection_sequence_last'],
            'stream_sha256':replay['input_stream_sha256'],
            'fresh_snapshots_per_product':{p:replay['per_product'][p]['snapshot_count'] for p in PRODUCTS},
            'reference_books':refs,
            'double_source_replay_agreed':True,
            'recovery_admission_gate':gate_result})
    return {'status':'TWO_REAL_SESSIONS_CONTROLLED_RECONNECT_INTERNAL_AUDIT_PASSED',
            'segments':results, 'controlled_reconnect_observed':True,
            'book_admission_gate_completed_connections':gate.completed_connections,
            'book_admission_gate_invalidated_connections':gate.invalidated_connections,
            'unplanned_disconnection_recovery_proven':False,
            'cross_connection_sequence_carried_forward':False,
            'independent_venue_atomic_book_reconciliation_proven':False,
            'historical_point_in_time_proven':False,
            'executable_alpha_proven':False,'orders_sent':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('folder',type=Path)
    p.add_argument('--out',type=Path)
    args=p.parse_args()
    result=audit_reconnect(args.folder)
    out=json.dumps(result,indent=2,sort_keys=True)+'\n'
    if args.out:args.out.write_text(out)
    print(out,end='')

if __name__=='__main__':main()
