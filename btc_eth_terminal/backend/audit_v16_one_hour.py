"""Pinned, read-only audit of the 2026-11-14 Coinbase BTC/ETH 3600s capture.

Two independent deterministic replay parsers validate the same source. A separate
streaming heartbeat/receive-gap census constrains descriptive availability.
This is NOT signed exchange truth, calibrated latency, fills, or economic alpha.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from statistics import quantiles
from audit_soak import audit
from independent_soak_replay import replay

SOURCE_DIRECTORY = 'coinbase_btc_eth_soak_eE9SNH'
SOURCE_MANIFEST_SHA = '08255d79f0a7f081921f370feca2e92686cde3dffbb406eadc23e918d43e73d7'
GITHUB_ARTIFACT_ZIP_SHA = '3f3829b66e93c991af86fda260c732e92bca40949fe91a1eb9b7b7ff0b3b6aa4'
GITHUB_RUN = 'https://github.com/minseogiiiii/quant-research-system/actions/runs/43572508160'
EXPECTED_MESSAGES = 152069
EXPECTED_CHUNKS = 41
BOOK_HASHES = {
    'BTC-USD': '84f40c5c3b7a7972722976430d8608715f3a14a344cb5a7ca5438289fc15d76f',
    'ETH-USD': '9957fe4da92506a1075c9f4ed14bda0e701745a107a6225c601608ce7a333b9d',
}

class OneHourEvidenceError(ValueError):
    pass


def _time(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def _seconds_summary(values):
    if not values:
        raise OneHourEvidenceError('Missing interval observations')
    ordered = sorted(values)
    return {
        'observations': len(ordered),
        'min_seconds': round(ordered[0], 6),
        'max_seconds': round(ordered[-1], 6),
        'median_seconds': round(ordered[len(ordered)//2], 6),
        'p99_seconds': round(ordered[min(len(ordered)-1, int(.99*(len(ordered)-1)))], 6),
    }


def _census(directory, manifest):
    channel_counts = Counter()
    by_minute = Counter()
    message_intervals = []
    heartbeat_intervals = []
    last_message = None
    last_heartbeat = None
    heartbeat_counter = None
    for item in manifest['chunks']:
        with (directory / item['file']).open('rb') as stream:
            for line in stream:
                row = json.loads(line)
                message = json.loads(row['raw_text'])
                receive = _time(row['received_utc'])
                channel = message['channel']
                channel_counts[channel] += 1
                by_minute[receive.strftime('%Y-%m-%dT%H:%MZ')] += 1
                if last_message is not None:
                    message_intervals.append((receive - last_message).total_seconds())
                last_message = receive
                if channel == 'heartbeats':
                    # The raw Coinbase counter is monitored for internal consistency,
                    # but its exact cadence may not be guaranteed by the venue.
                    if last_heartbeat is not None:
                        heartbeat_intervals.append((receive - last_heartbeat).total_seconds())
                    last_heartbeat = receive
                    events=message.get('events')
                    if not isinstance(events,list) or len(events)!=1 or not isinstance(events[0],dict):
                        raise OneHourEvidenceError('Malformed heartbeat events')
                    new_counter=events[0].get('heartbeat_counter')
                    if type(new_counter) is not int or new_counter < 0:
                        raise OneHourEvidenceError('Malformed provider heartbeat counter')
                    if heartbeat_counter is not None and new_counter != heartbeat_counter + 1:
                        raise OneHourEvidenceError('Missing/repeated public heartbeat counter')
                    heartbeat_counter=new_counter
    return {
        'last_heartbeat_counter': heartbeat_counter,
        'channel_counts': dict(sorted(channel_counts.items())),
        'heartbeat_receive_intervals': _seconds_summary(heartbeat_intervals),
        'all_message_receive_intervals': _seconds_summary(message_intervals),
        'receive_minutes_observed': len(by_minute),
        'min_messages_per_observed_minute': min(by_minute.values()),
        'max_messages_per_observed_minute': max(by_minute.values()),
        'source_clock_calibrated': False,
        'network_latency_measured': False,
        'heartbeat_cadence_only_descriptive': True,
    }


def audit_one_hour(directory: Path) -> dict:
    directory = Path(directory).resolve()
    if directory.name != SOURCE_DIRECTORY:
        raise OneHourEvidenceError('Unexpected archive directory identity')
    mf_bytes = (directory/'manifest.json').read_bytes()
    if sha256(mf_bytes).hexdigest() != SOURCE_MANIFEST_SHA:
        raise OneHourEvidenceError('One-hour original manifest digest changed')
    m = json.loads(mf_bytes)
    if not (m['required_duration_seconds'] == 3600
            and m['finish_reason'] == 'DURATION_REACHED'
            and m['status'] == 'CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH'
            and m['orders_enabled'] is False
            and m['strategy_execution_enabled'] is False
            and m['faults'] == []
            and m['total_reconnects'] == 0
            and len(m['connections']) == 1
            and m['connections'][0]['messages'] == EXPECTED_MESSAGES
            and m['connections'][0]['last_sequence'] == EXPECTED_MESSAGES-1
            and m['connections'][0]['integrity_fault'] is False
            and m['connections'][0]['became_ready'] is True):
        raise OneHourEvidenceError('Source 1h status, continuous sequence or safety scope differs')
    observed_period = (_time(m['finished_at_utc'])-_time(m['sampled_from_utc'])).total_seconds()
    if not 3600 <= observed_period <= 3605:
        raise OneHourEvidenceError('Source manifest did not cover 1h declared interval')
    primary = audit(directory)
    secondary = replay(directory)
    if not (primary['status'] == 'INTERNALLY_REPLAYED_NO_KNOWN_GAP'
            and primary['eligible_for_point_in_time_feature_research'] is True
            and primary['message_count'] == secondary['messages'] == EXPECTED_MESSAGES
            and primary['chunk_count'] == secondary['chunks'] == EXPECTED_CHUNKS
            and primary['connection_count'] == secondary['connections'] == 1
            and primary['connection_sequences'] == {'0': [0, EXPECTED_MESSAGES-1]}
            and primary['books_final'] == secondary['books_final']
            and all(primary['books_final'][p]['sha256'] == BOOK_HASHES[p] for p in BOOK_HASHES)):
        raise OneHourEvidenceError('Two replay implementations disagree on 1h source')
    for product in BOOK_HASHES:
        for key,value in secondary['per_product'][product].items():
            if primary['per_product'][product].get(key, 0) != value:
                raise OneHourEvidenceError('One-hour source trade/book counts disagree')
    if (m['resource_telemetry']['archived_jsonl_bytes'] != 325724466
            or m['bytes'] != 258396723
            or m['messages'] != EXPECTED_MESSAGES):
        raise OneHourEvidenceError('One-hour archived resource telemetry changed')
    census = _census(directory,m)
    if (census['channel_counts'].get('heartbeats') != 3599
            or census['channel_counts'].get('l2_data') <= 120000):
        raise OneHourEvidenceError('Source stream heartbeat/L2 totals outside pinned bounds')
    return {
        'status': 'ONE_HOUR_INTERNAL_SOURCE_DOUBLE_AUDIT_PASS',
        'verified': True,
        'observation_seconds': observed_period,
        'source': 'Coinbase Advanced Trade public WebSocket BTC-USD ETH-USD',
        'source_manifest_sha256': SOURCE_MANIFEST_SHA,
        'github_artifact_zip_sha256': GITHUB_ARTIFACT_ZIP_SHA,
        'github_actions_run': GITHUB_RUN,
        'source_messages': EXPECTED_MESSAGES,
        'chunks': EXPECTED_CHUNKS,
        'raw_text_bytes': m['bytes'],
        'archived_jsonl_bytes': m['resource_telemetry']['archived_jsonl_bytes'],
        'connection_count': 1,
        'recorded_source_sequence': [0, EXPECTED_MESSAGES-1],
        'faults': 0,
        'verified_l2_books': {p: primary['books_final'][p] for p in BOOK_HASHES},
        'per_product': primary['per_product'],
        'resource_telemetry': m['resource_telemetry'],
        'receive_time_census': census,
        'independent_exchange_book_truth_verified': False,
        'exchange_clock_synchronized': False,
        'actual_fills_verified': False,
        'live_order_execution_enabled': False,
        'economic_alpha_verified': False,
    }


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    result=audit_one_hour(args.directory)
    encoded=json.dumps(result,indent=2,sort_keys=True)+'\n'
    if args.output: args.output.write_text(encoded)
    print(encoded)


if __name__ == '__main__':main()
