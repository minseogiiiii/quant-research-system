"""Read-only, independent archival ingress gate for the existing BTC/ETH collector.

This module does not rebuild a book, ingest the live network, emit real-time
prices, or supersede the v26 independent crosscheck. It proves only that the
on-disk source bytes match an externally supplied manifest digest and satisfy
the original capture envelope's order/connection/snapshot contracts.

An admitted source is ALWAYS ARCHIVE_REPLAY, never an admitted live feed.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re

PRODUCTS = frozenset({'BTC-USD', 'ETH-USD'})
CHANNELS = frozenset({'l2_data', 'market_trades', 'heartbeats', 'subscriptions'})
MAX_MANIFEST_BYTES = 1_000_000
MAX_CHUNKS = 512
MAX_CHUNK_BYTES = 32_000_000
MAX_LINE_BYTES = 16_000_000
MAX_MESSAGES = 1_000_000
MAX_TOTAL_BYTES = 1_000_000_000
HEX64 = re.compile(r'[0-9a-f]{64}\Z')


class ArchiveRejected(ValueError):
    """No source promotion is possible once any invariant fails."""


def _reject(message: str):
    raise ArchiveRejected(message)


def _utc_ms(value: object) -> int:
    if not isinstance(value, str) or not value.endswith('Z'):
        _reject('NON_UTC_RECEIVER_CLOCK')
    try:
        t = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return int(t.timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        _reject('MALFORMED_RECEIVER_CLOCK')


def _digest(path: Path, limit: int) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file():
        _reject('MISSING_OR_SYMLINKED_EVIDENCE')
    if not 0 < path.stat().st_size <= limit:
        _reject('SOURCE_SIZE_OUT_OF_BOUNDS')
    h = sha256()
    count = 0
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            count += len(block)
            if count > limit:
                _reject('SOURCE_SIZE_OUT_OF_BOUNDS')
            h.update(block)
    return h.hexdigest(), count


def _chunk_path(root: Path, filename: object) -> Path:
    if not isinstance(filename, str):
        _reject('UNSAFE_CHUNK_NAME')
    p = PurePosixPath(filename)
    if (len(p.parts) != 2 or p.parts[0] != 'chunks' or
        not re.fullmatch(r'chunk_\d{5}_conn_\d{3}\.jsonl', p.parts[1])):
        _reject('UNSAFE_CHUNK_NAME')
    directory = root / 'chunks'
    if directory.is_symlink():
        _reject('SYMLINKED_CHUNK_DIRECTORY')
    return directory / p.parts[1]


def verify_capture(root: Path, expected_manifest_sha256: str) -> dict:
    """Returns a bounded, provenance-preserving certificate, never market state.

    The manifest pin MUST be supplied from an independent trusted inventory.
    This does not prove Coinbase exchange attestation, truth, uninterrupted
    market coverage, point-in-time model eligibility, or an executable alpha.
    """
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        _reject('INVALID_CAPTURE_DIRECTORY')
    if not isinstance(expected_manifest_sha256, str) or not HEX64.fullmatch(expected_manifest_sha256):
        _reject('MISSING_EXTERNAL_MANIFEST_PIN')
    manifest_path = root / 'manifest.json'
    manifest_hash, _ = _digest(manifest_path, MAX_MANIFEST_BYTES)
    if manifest_hash != expected_manifest_sha256:
        _reject('MANIFEST_PIN_MISMATCH')
    try:
        manifest = json.loads(manifest_path.read_bytes())
    except (UnicodeError, ValueError):
        _reject('MALFORMED_MANIFEST')
    if (not isinstance(manifest, dict) or manifest.get('format') != 'btc_eth_raw_soak_v10'
        or manifest.get('source') != 'wss://advanced-trade-ws.coinbase.com'
        or manifest.get('source_authentication') != 'PUBLIC_WEBSOCKET_NO_SIGNED_EXCHANGE_ATTESTATION'
        or set(manifest.get('products', [])) != PRODUCTS
        or manifest.get('sequence_scope') != 'PER_WEBSOCKET_CONNECTION'
        or manifest.get('orders_enabled') is not False
        or manifest.get('strategy_execution_enabled') is not False
        or manifest.get('point_in_time_historical_availability_verified') is not False
        or manifest.get('executable_alpha_verified') is not False):
        _reject('UNSUPPORTED_SOURCE_OR_TRADING_PERMISSIONS')
    chunks = manifest.get('chunks')
    conn_meta = manifest.get('connections')
    if (not isinstance(chunks, list) or not 1 <= len(chunks) <= MAX_CHUNKS or
        not isinstance(conn_meta, list) or not 1 <= len(conn_meta) <= 20):
        _reject('UNBOUNDED_SOURCE_INVENTORY')
    if any(not isinstance(c, dict) for c in chunks + conn_meta):
        _reject('MALFORMED_SOURCE_INVENTORY')
    if not all(type(c.get('id')) is int for c in conn_meta):
        _reject('INVALID_CONNECTION_IDS')
    if sorted(c['id'] for c in conn_meta) != list(range(len(conn_meta))):
        _reject('NONCONTIGUOUS_CONNECTION_IDS')
    states = {c['id']: {'next_seq': 0, 'count': 0, 'snapshot': set(), 'last_ms': None,
                         'last_monotonic': None} for c in conn_meta}
    seen_files = set()
    last_conn = 0
    totals = {'messages': 0, 'archived_bytes': 0, 'raw_text_bytes': 0}
    for idx, entry in enumerate(chunks):
        name = entry.get('file')
        path = _chunk_path(root, name)
        if path.name != f'chunk_{idx:05d}_conn_{entry.get("connection_id", -1):03}.jsonl':
            _reject('CHUNK_INDEX_OUT_OF_ORDER')
        if name in seen_files:
            _reject('DUPLICATE_SOURCE_CHUNK')
        seen_files.add(name)
        cid = entry.get('connection_id')
        if type(cid) is not int or cid not in states or cid < last_conn or cid > last_conn + 1:
            _reject('CONNECTION_BOUNDARY_OUT_OF_ORDER')
        if not re.fullmatch(r'chunk_\d{5}_conn_' + f'{cid:03}' + r'\.jsonl', path.name):
            _reject('CHUNK_CONNECTION_MISMATCH')
        last_conn = cid
        state = states[cid]
        size = entry.get('archived_line_bytes')
        expected_hash = entry.get('archive_sha256')
        if (type(size) is not int or not 0 < size <= MAX_CHUNK_BYTES or
            not isinstance(expected_hash, str) or not HEX64.fullmatch(expected_hash)):
            _reject('INVALID_CHUNK_METADATA')
        totals['archived_bytes'] += size
        if totals['archived_bytes'] > MAX_TOTAL_BYTES:
            _reject('TOTAL_ARCHIVE_EXCEEDS_BOUND')
        actual_hash, actual_size = _digest(path, MAX_CHUNK_BYTES)
        if actual_hash != expected_hash or actual_size != size:
            _reject('CHUNK_HASH_OR_LENGTH_MISMATCH')
        first_seq = state['next_seq']
        chunk_count = 0
        chunk_raw_text_bytes = 0
        first_ms = None
        last_ms = None
        with path.open('rb') as stream:
            for line in stream:
                if not 0 < len(line) <= MAX_LINE_BYTES:
                    _reject('UNBOUNDED_ARCHIVE_LINE')
                try:
                    envelope = json.loads(line)
                    raw_text = envelope['raw_text']
                    wire = json.loads(raw_text)
                except (ValueError, TypeError, KeyError, UnicodeError):
                    _reject('CORRUPT_WIRE_ENVELOPE')
                if (not isinstance(envelope, dict) or not isinstance(raw_text, str) or
                    not isinstance(wire, dict) or
                    envelope.get('sha256') != sha256(raw_text.encode()).hexdigest() or
                    type(envelope.get('connection_id')) is not int or
                    envelope['connection_id'] != cid):
                    _reject('ENVELOPE_DIGEST_OR_CONNECTION_MISMATCH')
                seq = wire.get('sequence_num')
                if type(seq) is not int or seq != state['next_seq']:
                    _reject('WIRE_SEQUENCE_GAP_OR_REORDER')
                state['next_seq'] += 1
                chunk_raw_text_bytes += len(raw_text.encode())
                mono_text = envelope.get('local_monotonic_ns')
                if not isinstance(mono_text, str) or not mono_text.isascii() or not mono_text.isdigit():
                    _reject('INVALID_MONOTONIC_CLOCK')
                mono = int(mono_text)
                ms = _utc_ms(envelope.get('received_utc'))
                if ((state['last_ms'] is not None and ms < state['last_ms']) or
                    (state['last_monotonic'] is not None and mono <= state['last_monotonic'])):
                    _reject('RECEIVER_ORDER_OR_MONOTONIC_REGRESSION')
                state['last_ms'], state['last_monotonic'] = ms, mono
                first_ms = ms if first_ms is None else first_ms
                last_ms = ms
                channel = wire.get('channel')
                if channel not in CHANNELS or not isinstance(wire.get('events'), list):
                    _reject('UNSUPPORTED_WIRE_CHANNEL')
                if channel == 'l2_data':
                    for event in wire['events']:
                        if not isinstance(event, dict) or event.get('product_id') not in PRODUCTS:
                            _reject('CROSS_PRODUCT_L2_EVENT')
                        product = event['product_id']
                        if event.get('type') == 'snapshot':
                            if not isinstance(event.get('updates'), list) or not event['updates']:
                                _reject('EMPTY_L2_SNAPSHOT')
                            state['snapshot'].add(product)
                        elif event.get('type') == 'update':
                            if product not in state['snapshot']:
                                _reject('UPDATE_BEFORE_CONNECTION_SNAPSHOT')
                        else:
                            _reject('UNSUPPORTED_L2_EVENT')
                chunk_count += 1
                totals['messages'] += 1
                state['count'] += 1
                if totals['messages'] > MAX_MESSAGES:
                    _reject('TOTAL_MESSAGES_EXCEED_BOUND')
        totals['raw_text_bytes'] += chunk_raw_text_bytes
        if (entry.get('raw_text_bytes') != chunk_raw_text_bytes or
            entry.get('messages') != chunk_count or entry.get('first_sequence') != first_seq or
            entry.get('last_sequence') != state['next_seq']-1 or
            _utc_ms(entry.get('first_received_utc')) != first_ms or
            _utc_ms(entry.get('last_received_utc')) != last_ms):
            _reject('CHUNK_INVENTORY_MISMATCH')
    if (type(manifest.get('bytes')) is not int or totals['raw_text_bytes'] != manifest['bytes']):
        _reject('CAPTURE_RAW_BYTES_MISMATCH')
    if (type(manifest.get('messages')) is not int or totals['messages'] != manifest['messages'] or
        sum(c['count'] for c in states.values()) != totals['messages']):
        _reject('CAPTURE_MESSAGE_COUNT_MISMATCH')
    for c in conn_meta:
        state = states[c['id']]
        if state['count'] != c.get('messages') or state['next_seq']-1 != c.get('last_sequence'):
            _reject('CONNECTION_INVENTORY_MISMATCH')
        if state['snapshot'] != PRODUCTS or c.get('snapshots') != {'BTC-USD': True, 'ETH-USD': True}:
            _reject('INCOMPLETE_CONNECTION_SNAPSHOT')
    clean = (manifest.get('status') == 'CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH'
             and manifest.get('ready_for_research') is True
             and manifest.get('complete_sequence_per_connection') is True
             and manifest.get('all_connections_have_snapshots') is True
             and manifest.get('faults') == []
             and all(c.get('integrity_fault') is False for c in conn_meta))
    return {'schema': 'v35_archive_ingress_v1', 'mode': 'ARCHIVE_REPLAY',
            'admission': 'VERIFIED_ARCHIVE_ENVELOPE' if clean else 'QUARANTINED_ARCHIVE_ENVELOPE',
            'manifest_sha256': manifest_hash, 'products': sorted(PRODUCTS),
            'connections': len(conn_meta), 'chunks': len(chunks),
            'messages': totals['messages'], 'archived_bytes': totals['archived_bytes'],
            'raw_text_bytes': totals['raw_text_bytes'],
            'receiver_time_only': True, 'live_feed': False, 'execution_authorized': False,
            'trading_enabled': False, 'original_feature_label_pit_verified': False,
            'independent_exchange_attestation': False, 'oos_approved': False,
            'source_eligible_for_state_replay': clean}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture_directory', type=Path)
    parser.add_argument('--manifest-sha256', required=True)
    args = parser.parse_args()
    try:
        result = verify_capture(args.capture_directory, args.manifest_sha256)
    except (ArchiveRejected, OSError) as exc:
        print(json.dumps({'admission': 'REJECTED', 'reason': str(exc), 'live_feed': False}))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result['source_eligible_for_state_replay'] else 1


if __name__ == '__main__':
    raise SystemExit(main())