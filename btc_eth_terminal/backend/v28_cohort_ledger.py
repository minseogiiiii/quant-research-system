"""Frozen independent-software source registry, not independent exchange attestation.

No source in this registry is promoted to a historically PIT-attested or
independent blind OOS research cohort. Each collector session is kept separate.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
from typing import Mapping

from audit_soak import audit as replay_a
from independent_soak_replay import replay as replay_b
from v27_github_public_smoke_audit import verify as verify_v27_artifact

SOURCE_PINS = (
 ('backend/data/live_chunked_180s_20261114/coinbase_btc_eth_soak_5XeK6R','4b86acc3c8d259b080f48bba55d815212089eb129daeb667e30cb87d2e3b1330'),
 ('backend/data/live_chunked_reverified_180s_20261114/coinbase_btc_eth_soak_5BJxvi','fb878c0f46a84cdc6a4595eb3adbbc23e35a93e26ba8130e7a918bbcacb3b5db'),
 ('backend/data/live_disk_safe_180s_20261114/coinbase_btc_eth_soak_CWFeG3','a904e7a0edb0aa9a295382a49ee0a7bfa2222312a949860eacbf43fe289d3dc7'),
 ('backend/data/live_disk_safe_600s_20261114/coinbase_btc_eth_soak_yRodga','3d4f41c180e88dddc40ffc592d3fb90528948c6f062aa6ca4dbde54dc929ee7f'),
 ('backend/data/live_disk_safe_final_180s_20261114/coinbase_btc_eth_soak_rV6Xox','8bda080658d08955f469d02469794f57f3fb6c610d7d92aeb760b42e412c3b3d'),
 ('backend/data/live_rolling_v13_120s_20261114/coinbase_btc_eth_soak_TX3aYK','563549235d6826c1319cae5b93b3d65dfe0d164ee7166588d552b9b8f447448c'),
 ('backend/data/live_rolling_v13_final_120s_20261114/coinbase_btc_eth_soak_mdkeBG','3ef1005c4775cd946badab0fc93ff01ddf0e49ab907882b08edd497a517ef29a'),
 ('backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy','8f2e0ec88a601a92ab4c7bcd0e04b20536fbe46bbd8b3c7f67ae3c1d075c28e5'),
 ('backend/data/live_v14_disconnect_120s/coinbase_btc_eth_soak_gEF6uZ','35e024fc87b317ebcf06c452562ef809a996bcc350c581356b7394c8022e5955'),
 ('backend/data/live_v16_one_hour/coinbase_btc_eth_soak_eE9SNH','08255d79f0a7f081921f370feca2e92686cde3dffbb406eadc23e918d43e73d7'),
 ('evidence/v27_github_real_60s/source/coinbase_btc_eth_soak_n9dQoG','a68a147322575274f455c02fcf5888df1d903cc7a4b21d6e5f8b4e71abc4e517'),
)
CLEAN_STATUS = 'CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH'
QUARANTINED_STATUS = 'CAPTURE_COMPLETED_WITH_QUARANTINED_GAPS'

class CohortLedgerError(ValueError):
    pass


def utc_time(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith('Z'):
        raise CohortLedgerError('UTC timestamp must be explicit Z')
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as e:
        raise CohortLedgerError('Invalid UTC timestamp') from e
    if dt.utcoffset() is None or dt.utcoffset().total_seconds() != 0:
        raise CohortLedgerError('Invalid UTC offset')
    return dt


def validate_pin(root: Path, rel: str, expected_sha: str):
    parts = PurePosixPath(rel).parts
    if (not parts or rel.startswith('/') or '\\' in rel or
            any(x in ('', '.', '..') for x in parts) or
            len(expected_sha) != 64):
        raise CohortLedgerError('Invalid source locator')
    here = root
    for part in parts:
        here = here / part
        if here.is_symlink():
            raise CohortLedgerError('Symlink source path')
    if not here.is_dir() or not here.resolve().is_relative_to(root.resolve()):
        raise CohortLedgerError('Missing or escaped source path')
    manifest_path = here / 'manifest.json'
    if (manifest_path.is_symlink() or not manifest_path.is_file() or
            manifest_path.stat().st_size > 5_000_000):
        raise CohortLedgerError('Missing or unsafe manifest')
    blob = manifest_path.read_bytes()
    if sha256(blob).hexdigest() != expected_sha:
        raise CohortLedgerError('Pinned source manifest changed: ' + rel)
    try:
        manifest = json.loads(blob)
    except (ValueError, UnicodeError) as e:
        raise CohortLedgerError('Invalid source manifest') from e
    start = utc_time(manifest['sampled_from_utc'])
    finish = utc_time(manifest['finished_at_utc'])
    if start >= finish:
        raise CohortLedgerError('Invalid capture interval')
    return here, manifest, start, finish


def verify_chunk_inventory(source: Path, manifest: dict) -> tuple[int, int]:
    """Check every raw archive member against the immutable manifest SHA, even without full replay."""
    chunks = manifest.get('chunks')
    if not isinstance(chunks, list) or not chunks or len(chunks) > 20000:
        raise CohortLedgerError('Unbounded or missing archive chunk list')
    seen = set()
    total_bytes = 0
    for chunk in chunks:
        if not isinstance(chunk, dict): raise CohortLedgerError('Invalid chunk')
        rel = chunk.get('file')
        if not isinstance(rel, str) or not rel.startswith('chunks/chunk_') or not rel.endswith('.jsonl'):
            raise CohortLedgerError('Invalid chunk name')
        parts = rel.split('/')
        if len(parts) != 2 or any(x in ('', '.', '..') for x in parts) or rel in seen:
            raise CohortLedgerError('Unsafe or duplicate chunk')
        seen.add(rel)
        chunk_path = source.joinpath(*parts)
        if (chunk_path.is_symlink() or not chunk_path.is_file() or
                not chunk_path.resolve().is_relative_to(source.resolve())):
            raise CohortLedgerError('Untrusted chunk file')
        size=chunk_path.stat().st_size
        if size <= 0 or size > 18_500_000 or size != chunk.get('archived_line_bytes'):
            raise CohortLedgerError('Chunk size mismatch')
        h=sha256()
        with chunk_path.open('rb') as reader:
            for b in iter(lambda:reader.read(1024*1024),b''):
                h.update(b)
        if h.hexdigest() != chunk.get('archive_sha256'):
            raise CohortLedgerError('Chunk SHA mismatch')
        total_bytes += size
    if len(seen) != len(list((source/'chunks').glob('*.jsonl'))):
        raise CohortLedgerError('Extra unregistered archive chunks')
    return len(seen), total_bytes

def overlapping_pairs(records: list[dict]) -> list[dict]:
    """Half-open [start,end); distinct captures can still overlap in real time."""
    intervals = []
    for c in records:
        start, finish = utc_time(c['start_utc']), utc_time(c['end_utc'])
        if start >= finish: raise CohortLedgerError('Malformed interval')
        intervals.append((start, finish, c['source_path']))
    intervals.sort(key=lambda v: (v[0], v[2]))
    return [{'first': left[2], 'second': right[2],
             'overlap_seconds': round((min(left[1], right[1])-max(left[0],right[0])).total_seconds(),3)}
            for i, left in enumerate(intervals) for right in intervals[i+1:]
            if left[0] < right[1] and right[0] < left[1]]


CRITICAL_REPLAY_TARGETS = frozenset(p for p,_ in SOURCE_PINS
    if 'live_v16_one_hour/' in p or 'live_v14_disconnect_' in p or 'v27_github_real_60s/' in p)

def cohort_summary(root: Path, pins=SOURCE_PINS, *, replay_targets=CRITICAL_REPLAY_TARGETS) -> dict:
    root = Path(root)
    if not root.is_dir() or root.is_symlink(): raise CohortLedgerError('Invalid project root')
    if len(set(rel for rel,_ in pins)) != len(pins): raise CohortLedgerError('Duplicate source path')
    if len(set(digest for _,digest in pins)) != len(pins): raise CohortLedgerError('Duplicate manifest identity')
    targets = set(replay_targets)
    if not targets.issubset({p for p,_ in pins}): raise CohortLedgerError('Unknown replay target')
    records = []
    for rel, pin in pins:
        source, m, start, end = validate_pin(root, rel, pin)
        # The only trusted source admission is software consistency; exchange-authenticity is unverified.
        checked_chunk_count, checked_archive_bytes = verify_chunk_inventory(source,m)
        if rel in targets:
            a, b = replay_a(source), replay_b(source)
            if (a.get('message_count') != b.get('messages') or
                    a.get('chunk_count') != b.get('chunks') or
                    a.get('connection_count') != b.get('connections') or
                    a.get('books_final') != b.get('books_final') or
                    a.get('message_count') != m.get('messages')):
                raise CohortLedgerError('Independent software disagreement: ' + rel)
            clean_replay = a['status'] == 'INTERNALLY_REPLAYED_NO_KNOWN_GAP'
        else:
            clean_replay = m.get('status') == CLEAN_STATUS
        if m.get('status') not in (CLEAN_STATUS, QUARANTINED_STATUS):
            raise CohortLedgerError('Unexpected capture status')
        if (m['source'] != 'wss://advanced-trade-ws.coinbase.com' or
                m['products'] != ['BTC-USD', 'ETH-USD'] or
                m['orders_enabled'] is not False):
            raise CohortLedgerError('Unexpected venue, products or orders')
        faulted = m['status'] != CLEAN_STATUS or bool(m.get('faults')) or m.get('total_reconnects',0)!=0
        if faulted and clean_replay: raise CohortLedgerError('Quarantine conflict')
        if not faulted and not clean_replay: raise CohortLedgerError('Unexpected replay rejection')
        records.append({'source_path':rel,'manifest_sha256':pin,
                        'start_utc':m['sampled_from_utc'],'end_utc':m['finished_at_utc'],
                        'interval_seconds_host_reported':round((end-start).total_seconds(),3),
                        'messages':m['messages'],'chunks':checked_chunk_count,
                        'sha256_verified_archive_bytes':checked_archive_bytes,
                        'all_chunk_hashes_verified':True,
                        'connections':len(m['connections']),
                        'capture_status':m['status'],
                        'internally_double_replayed':rel in targets,
                        'source_quarantined':faulted,
                        'clock_attested':False,'independent_exchange_truth_attested':False,
                        'blind_oos_eligible':False})
    overlaps = overlapping_pairs(records)
    new = next(c for c in records if c['source_path'].startswith('evidence/v27_github_real_60s/'))
    if any(x.startswith('evidence/v27_github_real_60s/') for x in targets):
        verify_v27_artifact(root/'evidence/v27_github_real_60s',root)
    clean=[x for x in records if not x['source_quarantined']]
    days=sorted({x['start_utc'][:10] for x in clean})
    return {'schema':'v28_immutable_source_cohort_ledger_v1',
            'status':'INTERNALLY_CROSSCHECKED_ENGINEERING_COHORTS_NO_PIT_OOS_ADMISSION',
            'cohorts':records,'cohort_count':len(records),
            'clean_collector_status_cohorts':len(clean),
            'complete_chunk_sha_verified_cohorts':len(records),
            'quarantined_cohorts':len(records)-len(clean),
            'unique_host_reported_calendar_days':days,
            'temporal_overlaps':overlaps,
            'overlapping_cohort_pairs':len(overlaps),
            'nonoverlap_is_required_for_independent_experiments':True,
            'source_messages_total_including_overlaps_and_quarantines':sum(x['messages'] for x in records),
            'sha256_verified_archive_bytes_total':sum(x['sha256_verified_archive_bytes'] for x in records),
            'new_v27_60s_manifest_sha256':new['manifest_sha256'],
            'new_v27_public_artifact_double_replayed':new['internally_double_replayed'],
            'double_replayed_cohort_count':sum(x['internally_double_replayed'] for x in records),
            'only_critical_cohorts_have_new_v28_double_replay':True,
            'historical_feature_label_bytes_recovered':False,
            'independent_host_clock_attestation':False,
            'continuous_single_session_24h_verified':False,
            'independent_blind_oos_eligible_cohorts':0,
            'live_orders':0,'executable_alpha_confirmed':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if args.output.exists() or args.output.is_symlink():
        p.error('Refusing to overwrite output')
    try:
        report=cohort_summary(args.root)
    except (OSError,ValueError,KeyError,TypeError) as e:
        report={'schema':'v28_immutable_source_cohort_ledger_v1','status':'REJECTED',
                'reason':str(e)[:500], 'executable_alpha_confirmed':False,'live_orders':0}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x') as fp: json.dump(report,fp,indent=2,sort_keys=True);fp.write('\n')
    print(report['status'])
    if report['status']=='REJECTED': raise SystemExit(2)

if __name__=='__main__':main()
