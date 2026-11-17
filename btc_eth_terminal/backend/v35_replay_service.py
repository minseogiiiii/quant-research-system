"""v35.1 read-only Coinbase archive replay adapter, reusing the frozen v23/v34 book.

Never admits a live source, trades, brokerage instructions, or fabricated observations.
Commit-on-success: no partial state escapes from failed validation/replay.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from hashlib import sha256
import heapq
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

from v35_archive_ingress import verify_capture, ArchiveRejected, MAX_CHUNK_BYTES
from v23_causal_microstructure import Book, FeatureReject, ofi_delta
from v34_l2_depth import top_levels, DepthError
from v21_source_evidence import iso_ms

PRODUCTS = ('BTC-USD', 'ETH-USD')
SAMPLE_MS = 1000
MAX_ROWS_PER_PRODUCT = 10000
MAX_LEVELS_PER_SIDE = 100_000
MAX_TRADES = 24
MAX_ENVELOPE_LINE = 16_000_000
CANDLE_MS = 60_000
MAX_COMPLETED_CANDLES = 2000

class StateRejected(ValueError):
    pass


def _secure_directory(root: Path) -> tuple[int, int]:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0)
    rootfd = os.open(root, flags)
    try:
        chunkfd = os.open('chunks', flags, dir_fd=rootfd)
    except BaseException:
        os.close(rootfd)
        raise
    return rootfd, chunkfd


def _read_fd(dirfd: int, filename: str, *, limit: int, expected_hash: str | None = None) -> bytes:
    if not re.fullmatch(r'[a-zA-Z0-9_.-]+', filename):
        raise StateRejected('UNSAFE_EVIDENCE_BASENAME')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
    fd = os.open(filename, flags, dir_fd=dirfd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
            raise StateRejected('SOURCE_SIZE_OR_KIND_CHANGED')
        output = bytearray()
        while True:
            chunk = os.read(fd, min(1 << 20, limit + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > limit:
                raise StateRejected('SOURCE_READ_EXCEEDS_BUDGET')
        if len(output) != info.st_size:
            raise StateRejected('SOURCE_SIZE_CHANGED_DURING_READ')
        raw = bytes(output)
        if expected_hash is not None and sha256(raw).hexdigest() != expected_hash:
            raise StateRejected('SOURCE_CHANGED_AFTER_ADMISSION')
        return raw
    finally:
        os.close(fd)


def _book_compact(book: Book) -> None:
    # The original v23 Book's heaps can accumulate stale duplicate prices after updates.
    # Pruning their cache preserves its exact price->quantity book semantics.
    for side in ('bid', 'offer'):
        levels = book.level[side]
        if len(levels) > MAX_LEVELS_PER_SIDE:
            raise StateRejected('MAX_BOOK_LEVELS_EXCEEDED')
        if len(book.heap[side]) > 4 * len(levels) + 2048:
            book.heap[side] = [(-p if side == 'bid' else p) for p in levels]
            heapq.heapify(book.heap[side])


def _features(book: Book, previous: Any, ofi: Decimal) -> dict:
    depth = top_levels(book)
    if depth is None:
        raise StateRejected('INCOMPLETE_TEN_LEVEL_BOOK')
    current = book.best()
    if current is None:
        raise StateRejected('MISSING_MARKET_QUOTE')
    (bid, qb), (ask, qa) = current
    spread = ask - bid
    if spread <= 0 or qb + qa <= 0:
        raise StateRejected('INVALID_SPREAD_OR_DEPTH')
    return {'best_bid': str(bid), 'best_ask': str(ask), 'spread': str(spread),
            'best_bid_qty': str(qb), 'best_ask_qty': str(qa),
            'microprice': str((ask * qb + bid * qa) / (qb + qa)),
            'best_imbalance': str((qb - qa) / (qb + qa)),
            'ofi_since_connection_base': str(ofi),
            'depth': depth, 'l2_book_levels': {side: len(book.level[side]) for side in ('bid', 'offer')}}


@dataclass(frozen=True)
class ReplayDataset:
    certificate: dict
    rows: dict[str, tuple[dict, ...]]
    final_state_sha256: str
    message_count: int
    candles: dict[str, tuple[dict, ...]] = field(default_factory=dict)

    def candles_page(self, product: str, limit: int = 400) -> dict:
        if product not in PRODUCTS:
            raise StateRejected('UNKNOWN_PRODUCT')
        if type(limit) is not int or not 1 <= limit <= 400:
            raise StateRejected('INVALID_CANDLE_LIMIT')
        records = self.candles.get(product, ())
        if len(records) > limit:
            raise StateRejected('CANDLE_PAGE_LIMIT_EXCEEDED')
        return {'schema': 'v35_1_archived_trade_candles_v1',
                'mode': 'ARCHIVE_REPLAY', 'product': product,
                'source_manifest_sha256': self.certificate['manifest_sha256'],
                'timeframe_ms': CANDLE_MS,
                'source_definition': 'RECEIVER_TIME_COMPLETED_BUCKETS_OBSERVED_TRADE_UPDATES_ONLY',
                'rows': list(records), 'live_feed': False,
                'trading_enabled': False, 'oos_approved': False}

    def snapshot(self, product: str, at_version: int | None = None) -> dict:
        if product not in PRODUCTS:
            raise StateRejected('UNKNOWN_PRODUCT')
        records = self.rows[product]
        if not records:
            raise StateRejected('NO_ADMISSIBLE_PRODUCT_SNAPSHOT')
        if at_version is None:
            row = records[-1]
        else:
            if type(at_version) is not int or at_version < 1:
                raise StateRejected('INVALID_STATE_VERSION')
            row = next((r for r in reversed(records) if r['state_version'] <= at_version), None)
            if row is None:
                raise StateRejected('STATE_VERSION_NOT_RETAINED')
        return {'schema': 'v35_1_replay_snapshot_v1', 'mode': 'ARCHIVE_REPLAY',
                'source_admission': self.certificate['admission'],
                'source_manifest_sha256': self.certificate['manifest_sha256'],
                'live_feed': False, 'trading_enabled': False, 'execution_authorized': False,
                'independent_exchange_attestation': False,
                'state': row}

    def page(self, product: str, after: int = 0, limit: int = 100) -> dict:
        if product not in PRODUCTS:
            raise StateRejected('UNKNOWN_PRODUCT')
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 250:
            raise StateRejected('INVALID_PAGE_ARGUMENTS')
        rows = self.rows[product]
        if after:
            known = {r['state_version'] for r in rows}
            if rows and after < rows[0]['state_version']:
                raise StateRejected('RESYNC_REQUIRED')
            if after not in known:
                raise StateRejected('RESYNC_REQUIRED')
        visible = [r for r in rows if r['state_version'] > after][:limit]
        return {'schema': 'v35_1_replay_page_v1', 'mode': 'ARCHIVE_REPLAY',
                'product': product, 'source_manifest_sha256': self.certificate['manifest_sha256'],
                'rows': visible, 'has_more': bool(visible and visible[-1]['state_version'] < rows[-1]['state_version']),
                'next_after': visible[-1]['state_version'] if visible else after,
                'live_feed': False, 'trading_enabled': False}


def replay_capture(root: Path, expected_manifest_sha256: str) -> ReplayDataset:
    """Verifies ingress, then re-hashes exact fd-backed bytes before each use.

    The complete result is committed atomically only after *all* original chunks pass.
    """
    try:
        certificate = verify_capture(root, expected_manifest_sha256)
        if not certificate.get('source_eligible_for_state_replay') or certificate.get('mode') != 'ARCHIVE_REPLAY':
            raise StateRejected('QUARANTINED_ARCHIVE_HAS_NO_TRUSTED_MARKET_STATE')
        rootfd, chunkfd = _secure_directory(Path(root))
        try:
            manifest_raw = _read_fd(rootfd, 'manifest.json', limit=1_000_000,
                                    expected_hash=expected_manifest_sha256)
            manifest = json.loads(manifest_raw)
            if len(manifest['chunks']) != certificate['chunks']:
                raise StateRejected('POST_ADMISSION_MANIFEST_CHANGED')
            books = {product: Book() for product in PRODUCTS}
            last_seen = {product: None for product in PRODUCTS}
            ofi = {product: Decimal(0) for product in PRODUCTS}
            last_published = {product: None for product in PRODUCTS}
            output: dict[str, list[dict]] = {product: [] for product in PRODUCTS}
            recent_trades: dict[str, list[dict]] = {product: [] for product in PRODUCTS}
            expected_seq = 0
            last_conn = 0
            state_version = 0
            message_count = 0
            last_message_recv_ms = None
            first_message_recv_ms = None
            trade_candle_buckets = {product: {} for product in PRODUCTS}
            for index, entry in enumerate(manifest['chunks']):
                conn = entry['connection_id']
                if conn != last_conn:
                    if conn != last_conn + 1:
                        raise StateRejected('UNEXPECTED_CONNECTION_TRANSITION')
                    books = {product: Book() for product in PRODUCTS}
                    last_seen = {product: None for product in PRODUCTS}
                    ofi = {product: Decimal(0) for product in PRODUCTS}
                    last_published = {product: None for product in PRODUCTS}
                    recent_trades = {product: [] for product in PRODUCTS}
                    expected_seq = 0
                    last_conn = conn
                expected_name = f'chunk_{index:05d}_conn_{conn:03d}.jsonl'
                if entry['file'] != 'chunks/' + expected_name:
                    raise StateRejected('UNEXPECTED_SOURCE_CHUNK_NAME')
                raw = _read_fd(chunkfd, expected_name, limit=MAX_CHUNK_BYTES,
                               expected_hash=entry['archive_sha256'])
                if len(raw) != entry['archived_line_bytes']:
                    raise StateRejected('SOURCE_LENGTH_CHANGED_AFTER_ADMISSION')
                for line in raw.splitlines(keepends=True):
                    if not 0 < len(line) <= MAX_ENVELOPE_LINE:
                        raise StateRejected('SOURCE_LINE_EXCEEDS_BUDGET')
                    envelope = json.loads(line)
                    wire_raw = envelope['raw_text']
                    if sha256(wire_raw.encode()).hexdigest() != envelope['sha256']:
                        raise StateRejected('WIRE_PAYLOAD_CHANGED_AFTER_ADMISSION')
                    wire = json.loads(wire_raw)
                    seq = wire['sequence_num']
                    if seq != expected_seq or envelope['connection_id'] != conn:
                        raise StateRejected('WIRE_SEQUENCE_CHANGED_AFTER_ADMISSION')
                    expected_seq += 1
                    message_count += 1
                    recv_ms = iso_ms(envelope['received_utc'])
                    last_message_recv_ms = recv_ms
                    if first_message_recv_ms is None:
                        first_message_recv_ms = recv_ms
                    channel = wire['channel']
                    if channel == 'market_trades':
                        for event in wire['events']:
                            if event.get('type') not in ('snapshot', 'update'):
                                raise StateRejected('UNSUPPORTED_TRADE_EVENT_KIND')
                            for trade in event.get('trades', []):
                                product = trade.get('product_id')
                                if product not in PRODUCTS:
                                    raise StateRejected('UNEXPECTED_TRADE_PRODUCT')
                                # Venue event times are not used for availability.
                                recent_trades[product].append({
                                    'price': trade['price'], 'size': trade['size'],
                                    'side': trade.get('side'), 'trade_id': trade.get('trade_id'),
                                    'received_at_ms': recv_ms,
                                })
                                del recent_trades[product][:-MAX_TRADES]
                                # The startup trade snapshot is historical context, not a
                                # newly observed trade flow event. Exclude it from OHLCV.
                                if event['type'] != 'update':
                                    continue
                                try:
                                    price = Decimal(trade['price'])
                                    size = Decimal(trade['size'])
                                except (ValueError, ArithmeticError, KeyError, TypeError) as exc:
                                    raise StateRejected('INVALID_TRADE_DECIMAL') from exc
                                if not price.is_finite() or not size.is_finite() or price <= 0 or size <= 0:
                                    raise StateRejected('NONPOSITIVE_TRADE_DECIMAL')
                                start = (recv_ms // CANDLE_MS) * CANDLE_MS
                                bucket = trade_candle_buckets[product]
                                bar = bucket.get(start)
                                if bar is None:
                                    if len(bucket) >= MAX_COMPLETED_CANDLES + 2:
                                        raise StateRejected('MAX_TRADE_CANDLE_BUCKETS_EXCEEDED')
                                    bucket[start] = {'open': price, 'high': price,
                                                     'low': price, 'close': price,
                                                     'volume': size, 'observed_trade_count': 1}
                                else:
                                    bar['high'] = max(bar['high'], price)
                                    bar['low'] = min(bar['low'], price)
                                    bar['close'] = price
                                    bar['volume'] += size
                                    bar['observed_trade_count'] += 1
                    if channel != 'l2_data':
                        continue
                    for event in wire['events']:
                        product = event['product_id']
                        if product not in PRODUCTS:
                            raise StateRejected('UNEXPECTED_L2_PRODUCT')
                        book = books[product]
                        before = book.best()
                        kind = event['type']
                        book.apply(kind, event['updates'])
                        _book_compact(book)
                        after = book.best()
                        if after is None:
                            raise StateRejected('L2_BOOK_NOT_READY_AFTER_EVENT')
                        if kind == 'update':
                            ofi[product] += ofi_delta(before, after)
                        last_seen[product] = (recv_ms, seq, conn)
                        if last_published[product] is not None and recv_ms - last_published[product] < SAMPLE_MS:
                            continue
                        features = _features(book, before, ofi[product])
                        state_version += 1
                        row = {'schema': 'v35_1_market_state_v1', 'product': product,
                               'venue': 'Coinbase Advanced public spot', 'mode': 'ARCHIVE_REPLAY',
                               'connection_id': conn, 'source_sequence': seq,
                               'receiver_time_ms': recv_ms, 'source_last_l2_receiver_ms': recv_ms,
                               'source_age_ms': 0, 'state_version': state_version,
                               'source_manifest_sha256': expected_manifest_sha256,
                               'archive_admission': certificate['admission'],
                               'freshness': 'ARCHIVED_NOT_LIVE', 'status': 'VALID_ARCHIVED_QUOTE',
                               'trading_enabled': False, 'live_feed': False,
                               'recent_trades': list(recent_trades[product]), **features}
                        output[product].append(row)
                        if len(output[product]) > MAX_ROWS_PER_PRODUCT:
                            raise StateRejected('BOUNDED_STATE_HISTORY_EXCEEDED')
                        last_published[product] = recv_ms
            # Capture the last receiver-known quote even when the final update
            # occurred less than SAMPLE_MS after the preceding periodic publication.
            for product in PRODUCTS:
                seen = last_seen[product]
                if seen is None:
                    raise StateRejected('MISSING_FINAL_L2_PRODUCT')
                recv_ms, seq, conn = seen
                if last_published[product] == last_message_recv_ms:
                    continue
                age = last_message_recv_ms - recv_ms
                if age < 0:
                    raise StateRejected('FINAL_RECEIVER_CLOCK_REGRESSION')
                too_old = age > 5000
                state_version += 1
                output[product].append({
                    'schema': 'v35_1_market_state_v1', 'product': product,
                    'venue': 'Coinbase Advanced public spot', 'mode': 'ARCHIVE_REPLAY',
                    'connection_id': conn, 'source_sequence': seq,
                    'receiver_time_ms': last_message_recv_ms,
                    'source_last_l2_receiver_ms': recv_ms, 'source_age_ms': age,
                    'state_version': state_version,
                    'source_manifest_sha256': expected_manifest_sha256,
                    'archive_admission': certificate['admission'],
                    'freshness': 'ARCHIVED_STALE' if too_old else 'ARCHIVED_NOT_LIVE',
                    'trading_enabled': False, 'live_feed': False,
                    'recent_trades': list(recent_trades[product]),
                    'status': 'NO_ADMISSIBLE_QUOTE' if too_old else 'VALID_ARCHIVED_QUOTE',
                    **({} if too_old else _features(books[product], None, ofi[product]))})
            if message_count != certificate['messages']:
                raise StateRejected('FINAL_REPLAY_COUNT_MISMATCH')
            if not all(output.values()):
                raise StateRejected('INCOMPLETE_DUAL_PRODUCT_REPLAY')
            rows = {p: tuple(output[p]) for p in PRODUCTS}
            candles: dict[str, tuple[dict, ...]] = {}
            if first_message_recv_ms is None or last_message_recv_ms is None:
                raise StateRejected('EMPTY_SOURCE_CLOCK_RANGE')
            earliest_complete = ((first_message_recv_ms + CANDLE_MS - 1) // CANDLE_MS) * CANDLE_MS
            for product in PRODUCTS:
                completed = []
                for start, data in sorted(trade_candle_buckets[product].items()):
                    if start < earliest_complete or start + CANDLE_MS > last_message_recv_ms:
                        continue
                    completed.append({
                        'schema': 'v35_1_receiver_trade_bar_v1', 'product': product,
                        'mode': 'ARCHIVE_REPLAY', 'live_feed': False, 'trading_enabled': False,
                        'source_manifest_sha256': expected_manifest_sha256,
                        'time': start, 'available_at': start + CANDLE_MS,
                        'open': str(data['open']), 'high': str(data['high']),
                        'low': str(data['low']), 'close': str(data['close']),
                        'volume': str(data['volume']),
                        'observed_trade_count': data['observed_trade_count'],
                        'source_definition': 'RECEIVER_TIME_COMPLETED_BUCKET_OBSERVED_TRADE_UPDATES_ONLY'})
                if len(completed) > MAX_COMPLETED_CANDLES:
                    raise StateRejected('MAX_COMPLETED_CANDLES_EXCEEDED')
                candles[product] = tuple(completed)
            raw = json.dumps({'rows': rows, 'candles': candles}, sort_keys=True,
                             separators=(',', ':')).encode()
            return ReplayDataset(certificate, rows, sha256(raw).hexdigest(), message_count, candles)
        finally:
            os.close(chunkfd)
            os.close(rootfd)
    except (ArchiveRejected, FeatureReject, DepthError, OSError, KeyError, TypeError,
            ValueError, OverflowError, IndexError) as exc:
        if isinstance(exc, StateRejected):
            raise
        raise StateRejected(f'REPLAY_REJECTED: {exc}') from exc