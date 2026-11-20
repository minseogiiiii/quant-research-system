"""Create a factual, offline BTC/ETH source health report for a future UI.

This is a read-only archived-source status, never a LIVE/OPEN-NOW trade signal.
A collected file can be internally replayable without being authentic, current,
clock-synchronized, or sufficient for profitable/legally permitted orders.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from audit_soak import audit, SoakAuditError


def report(folder: Path) -> dict:
    source = Path(folder)
    base = {
        'market_scope': ['BTC-USD Coinbase SPOT', 'ETH-USD Coinbase SPOT'],
        'read_only': True,
        'session_type': 'ARCHIVED_SOURCE_EVIDENCE_NOT_LIVE_FEED',
        'available_to_trade': False,
        'live_orders_enabled': False,
        'historical_point_in_time_availability_proven': False,
        'external_reference_book_sequence_matched': False,
        'exchange_clock_calibrated': False,
        'executable_alpha_verified': False,
    }
    try:
        verified = audit(source)
        manifest = json.loads((source / 'manifest.json').read_bytes())
        telemetry = manifest.get('resource_telemetry')
        if verified['status'] != 'INTERNALLY_REPLAYED_NO_KNOWN_GAP':
            raise SoakAuditError('Source is archived but not admitted for offline research')
        result={
            **base,
            'status': 'ARCHIVED_SOURCE_INTERNALLY_REPLAYED',
            'can_use_for_archived_data_integrity_research': True,
            'recorded_from_utc': manifest['sampled_from_utc'],
            'recorded_until_utc': manifest['finished_at_utc'],
            'connection_count': verified['connection_count'],
            'recorded_messages': verified['message_count'],
            'recorded_raw_text_bytes': verified['raw_text_bytes'],
            'durable_chunk_count': verified['chunk_count'],
            'source_sequences': verified['connection_sequences'],
            'market_books_at_end': verified['books_final'],
            'source_chunk_sha256': [c['archive_sha256'] for c in manifest['chunks']],
            'recording_machine_telemetry_unverified_external': telemetry,
            'live_feed_freshness_status': 'NOT_A_LIVE_CONNECTION',
            'source_integrity_note': 'Double replay/cryptographic hashes prove only internal source consistency.',
        }
    except (ValueError, OSError, KeyError, TypeError) as exc:
        result={**base,
            'status': 'SOURCE_HEALTH_BLOCKED',
            'can_use_for_archived_data_integrity_research': False,
            'live_feed_freshness_status': 'UNKNOWN_OR_BAD_SOURCE',
            'error': str(exc)[:500]}
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    result=report(args.archive)
    if args.output:
        args.output.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    print(json.dumps(result,indent=2,sort_keys=True))
    if result['status'] != 'ARCHIVED_SOURCE_INTERNALLY_REPLAYED':
        raise SystemExit(1)


if __name__=='__main__':main()
