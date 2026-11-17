"""Second, independent interpretation of archived Coinbase public BTC/ETH L2.

Intentional design: no calls to the primary `audit_soak` functions. Agreement
means two deterministic replay implementations agree on the *same* source,
NOT that any exchange order could have filled or the book is independently
venue-attested. Decimal arithmetic; no PyPI dependency or broker endpoints.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import heapq
import json
from pathlib import Path
import re

SYMBOLS = ('BTC-USD', 'ETH-USD')


class IndependentReplayError(ValueError):
    pass


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise IndependentReplayError('duplicate key')
            result[key] = value
        return result

    def bad_constant(text):
        raise IndependentReplayError('non-JSON numeric constant: ' + text)

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=bad_constant)


def price(text, zero=False):
    if not isinstance(text, str) or not text.strip():
        raise IndependentReplayError('invalid decimal source')
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise IndependentReplayError('invalid numeric source') from exc
    if not amount.is_finite() or amount < 0 or (not zero and amount == 0):
        raise IndependentReplayError('negative/nonfinite/zero price or quantity')
    return amount


def parse_utc(text):
    if not isinstance(text, str):
        raise IndependentReplayError('missing UTC timestamp')
    dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise IndependentReplayError('non-UTC timestamp')
    return dt



class PriceLadderIndex:
    """Only a book-price index. The exact book state stays in independent dicts."""
    def __init__(self):
        self.bids = []
        self.asks = []

    def record_added(self, side, px):
        heapq.heappush(self.bids if side == 'bid' else self.asks,
                       -px if side == 'bid' else px)

    def check(self, book):
        for side, heap in (('bid', self.bids), ('offer', self.asks)):
            active = book[side]
            while heap and ((-heap[0] if side == 'bid' else heap[0]) not in active):
                heapq.heappop(heap)
            if active and not heap:
                raise IndependentReplayError('Inconsistent active price index')
            if len(heap) > 3 * len(active) + 1024:
                heap[:] = [-x if side == 'bid' else x for x in active]
                heapq.heapify(heap)
        if self.bids and self.asks and -self.bids[0] >= self.asks[0]:
            raise IndependentReplayError('crossed/locked order book')

def replay(folder: Path):
    folder = Path(folder).resolve()
    mf = strict_json((folder / 'manifest.json').read_bytes())
    if (mf['format'] != 'btc_eth_raw_soak_v10'
            or mf['source'] != 'wss://advanced-trade-ws.coinbase.com'
            or mf['products'] != list(SYMBOLS)
            or mf['orders_enabled'] is not False
            or mf['executable_alpha_verified'] is not False):
        raise IndependentReplayError('source provenance/safety conflict')
    chunks = mf['chunks']
    if not chunks or len(chunks) > 10000:
        raise IndependentReplayError('empty or too large source manifest')
    books = {p: {'bid': {}, 'offer': {}} for p in SYMBOLS}
    price_indexes = {p: PriceLadderIndex() for p in SYMBOLS}
    conn = {}
    counts = {p: dict(snapshot=0, update=0, historical_trade_snapshot=0,
                      unique_update_trade=0, unique_trade_id=0,
                      identical_update_trade_replays_excluded=0) for p in SYMBOLS}
    nrows = 0
    nbytes = 0
    for index, info in enumerate(chunks):
        cid = info['connection_id']
        if type(cid) is not int or cid < 0 or cid >= len(mf['connections']):
            raise IndependentReplayError('unknown connection')
        expected = f'chunks/chunk_{index:05d}_conn_{cid:03d}.jsonl'
        if info['file'] != expected or not re.fullmatch(r'chunks/chunk_\d{5}_conn_\d{3}\.jsonl', expected):
            raise IndependentReplayError('unexpected chunk path')
        file = folder / expected
        if file.is_symlink() or not file.is_file() or not file.resolve().is_relative_to(folder):
            raise IndependentReplayError('unsafe source path')
        if file.stat().st_size > 18_500_000 or file.stat().st_size == 0:
            raise IndependentReplayError('oversized/empty file')
        if cid not in conn:
            conn[cid] = {'seq': -1, 'snapshot': set(), 'heartbeat': 0,
                         'trades': {p: {} for p in SYMBOLS},
                         'messages': 0, 'updates': {p: 0 for p in SYMBOLS}}
        state = conn[cid]
        digest = hashlib.sha256()
        rows = 0
        rawbytes = 0
        first_seq = first_received = last_received = None
        with file.open('rb') as stream:
            for line in stream:
                if not line.endswith(b'\n') or len(line) > 18_500_000:
                    raise IndependentReplayError('broken source line')
                digest.update(line)
                packet = strict_json(line)
                if packet['connection_id'] != cid:
                    raise IndependentReplayError('connection mismatch')
                raw = packet['raw_text']
                if not isinstance(raw, str):
                    raise IndependentReplayError('missing UTF-8 text')
                payload = raw.encode('utf-8')
                if hashlib.sha256(payload).hexdigest() != packet['sha256']:
                    raise IndependentReplayError('message digest mismatch')
                rawbytes += len(payload)
                event = strict_json(payload)
                seq = event['sequence_num']
                if type(seq) is not int or seq != state['seq'] + 1:
                    raise IndependentReplayError('sequence replay/gap')
                state['seq'] = seq
                first_seq = seq if first_seq is None else first_seq
                observed = parse_utc(packet['received_utc'])
                parse_utc(event['timestamp'])
                if first_received is None:
                    first_received = observed
                last_received = observed
                channel = event['channel']
                if channel == 'l2_data':
                    for item in event['events']:
                        prod = item['product_id']
                        if prod not in SYMBOLS:
                            raise IndependentReplayError('unexpected product')
                        if item['type'] == 'snapshot':
                            if prod in state['snapshot']:
                                raise IndependentReplayError('duplicate snapshot')
                            state['snapshot'].add(prod)
                            books[prod] = {'bid': {}, 'offer': {}}
                            price_indexes[prod] = PriceLadderIndex()
                            counts[prod]['snapshot'] += 1
                        elif item['type'] == 'update':
                            if prod not in state['snapshot']:
                                raise IndependentReplayError('update before source snapshot')
                            counts[prod]['update'] += 1
                            state['updates'][prod] += 1
                        else:
                            raise IndependentReplayError('unknown book event')
                        for level in item['updates']:
                            side = level['side']
                            if side not in ('bid', 'offer'):
                                raise IndependentReplayError('bad L2 side')
                            px = price(level['price_level'])
                            qty = price(level['new_quantity'], zero=True)
                            parse_utc(level['event_time'])
                            active = books[prod][side]
                            if qty == 0:
                                active.pop(px, None)
                            else:
                                new_level = px not in active
                                active[px] = qty
                                if new_level:
                                    price_indexes[prod].record_added(side, px)
                        price_indexes[prod].check(books[prod])
                elif channel == 'market_trades':
                    for item in event['events']:
                        kind = item['type']
                        if kind not in ('snapshot', 'update'):
                            raise IndependentReplayError('unknown trade event')
                        for tr in item['trades']:
                            prod = tr['product_id']
                            if prod not in SYMBOLS:
                                raise IndependentReplayError('unknown trade symbol')
                            if tr['side'] not in ('BUY', 'SELL'):
                                raise IndependentReplayError('trade side')
                            price(tr['price']);price(tr['size']);parse_utc(tr['time'])
                            tradeid = tr['trade_id']
                            if not isinstance(tradeid, str) or not tradeid:
                                raise IndependentReplayError('empty trade ID')
                            signature = (kind, tuple(sorted(tr.items())))
                            already = state['trades'][prod].get(tradeid)
                            if already is not None:
                                oldseq, oldsig = already
                                if kind != 'update' or oldseq == seq or oldsig != signature:
                                    raise IndependentReplayError('conflicting duplicate trade ID')
                                counts[prod]['identical_update_trade_replays_excluded'] += 1
                            else:
                                state['trades'][prod][tradeid] = (seq, signature)
                                counts[prod]['unique_trade_id'] += 1
                                counts[prod]['historical_trade_snapshot' if kind == 'snapshot'
                                             else 'unique_update_trade'] += 1
                elif channel == 'heartbeats':
                    state['heartbeat'] += 1
                elif channel != 'subscriptions':
                    raise IndependentReplayError('unrecognized source channel')
                nrows += 1
                rows += 1
                state['messages'] += 1
        if (info['archive_sha256'] != digest.hexdigest() or rows != info['messages']
                or rawbytes != info['raw_text_bytes'] or file.stat().st_size != info['archived_line_bytes']
                or first_seq != info['first_sequence'] or state['seq'] != info['last_sequence']
                or first_received.isoformat() != parse_utc(info['first_received_utc']).isoformat()
                or last_received.isoformat() != parse_utc(info['last_received_utc']).isoformat()):
            raise IndependentReplayError('chunk source/metadata mismatch')
        nbytes += rawbytes
    for c in mf['connections']:
        state = conn.get(c['id'])
        if state is None or state['messages'] != c['messages'] or state['seq'] != c['last_sequence']:
            raise IndependentReplayError('connection count mismatch')
        if ({p: p in state['snapshot'] for p in SYMBOLS} != c['snapshots']
                or state['updates'] != c['updates'] or state['heartbeat'] != c['heartbeat_count']):
            raise IndependentReplayError('connection source metadata mismatch')
    if nrows != mf['messages'] or nbytes != mf['bytes']:
        raise IndependentReplayError('total source size mismatch')
    def book_summary(book):
        levels = [(side, format(px.normalize(),'f'), format(qty.normalize(),'f'))
                  for side in ('bid','offer') for px,qty in sorted(book[side].items())]
        return {'best_bid': format(max(book['bid']).normalize(),'f') if book['bid'] else None,
                'best_ask': format(min(book['offer']).normalize(),'f') if book['offer'] else None,
                'bid_levels': len(book['bid']),'ask_levels':len(book['offer']),
                'sha256':hashlib.sha256(json.dumps(levels,separators=(',',':')).encode()).hexdigest()}
    return {'status':'INDEPENDENT_SOURCE_REPLAY_ONLY','messages':nrows,'chunks':len(chunks),
            'connections':len(conn),'per_product':counts,
            'books_final':{p:book_summary(books[p]) for p in SYMBOLS},
            'source_authentication_verified':False,'executable_alpha_verified':False}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('source', type=Path)
    ap.add_argument('--report', type=Path)
    args = ap.parse_args()
    result = replay(args.source)
    output = json.dumps(result, sort_keys=True, indent=2)+'\n'
    print(output)
    if args.report:
        args.report.write_text(output)

if __name__ == '__main__':
    main()