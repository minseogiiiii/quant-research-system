"""v26 fail-closed source + operator + periodic host clock admission crosscheck.

Independent replay refers to independent SOFTWARE implementations over the
same bytes, not independent exchange truth. No real order interfaces.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path

from v25_capture_admission import assess_archive, load_json
from v26_clock_trace import ClockTraceError, inspect_trace


class CrosscheckError(ValueError):
    pass


def crosscheck(source: Path, evidence_dir: Path):
    source = Path(source)
    evidence_dir = Path(evidence_dir)
    if (source.is_symlink() or not source.is_dir() or evidence_dir.is_symlink()
            or not evidence_dir.is_dir()):
        raise CrosscheckError('UNSAFE_SOURCE_OR_EVIDENCE_DIRECTORY')
    operator_path = evidence_dir / 'V25_OPERATOR_EVIDENCE.json'
    context_path = evidence_dir / 'V26_TRACE_CONTEXT.json'
    trace_path = evidence_dir / 'V26_CLOCK_TRACE.jsonl'
    op = load_json(operator_path)
    context = load_json(context_path)
    if not isinstance(op, dict) or not isinstance(context, dict):
        raise CrosscheckError('MALFORMED_CONTEXT_OR_OPERATOR')
    if context.get('schema') != 'v26_clock_trace_context_v1':
        raise CrosscheckError('INVALID_V26_CONTEXT_SCHEMA')
    if context.get('source_archive_dir') != str(source.resolve()):
        raise CrosscheckError('V26_SOURCE_DIRECTORY_NOT_BOUND')
    if op.get('source_archive_dir') != str(source.resolve()):
        raise CrosscheckError('V25_SOURCE_DIRECTORY_NOT_BOUND')
    if context.get('run_mode') != op.get('run_mode'):
        raise CrosscheckError('RUN_MODE_NOT_BOUND')
    if context.get('archive_manifest_sha256') != op.get('archive_manifest_sha256'):
        raise CrosscheckError('MANIFEST_HASH_NOT_BOUND')
    if context.get('operator_evidence_sha256') != sha256(operator_path.read_bytes()).hexdigest():
        raise CrosscheckError('OPERATOR_EVIDENCE_HASH_NOT_BOUND')
    if (trace_path.is_symlink() or not trace_path.is_file() or
            trace_path.stat().st_size > 600*24_000):
        raise CrosscheckError('TRACE_FILE_UNSAFE_OR_OVERSIZED')
    if context.get('clock_trace_sha256') != sha256(trace_path.read_bytes()).hexdigest():
        raise CrosscheckError('CLOCK_TRACE_BYTES_NOT_BOUND')
    if ('capture_failure' not in op or 'capture_failure' not in context or
            op['capture_failure'] is not None or context['capture_failure'] is not None or
            op.get('capture_exit_code') != 0 or op.get('orders_enabled') is not False):
        raise CrosscheckError('OPERATOR_FAILURE_OR_MISSING_CAPTURE_CONTRACT')
    clock = inspect_trace(trace_path, op, mode=op.get('run_mode'))
    if context.get('trace_samples') != clock['samples']:
        raise CrosscheckError('TRACE_SAMPLE_COUNT_UNBOUND')
    reserve=context.get('disk_reserve_bytes')
    if type(reserve) is not int or not 0 <= reserve <= 10**12:
        raise CrosscheckError('DISK_RESERVE_UNBOUND')
    if clock['min_free_storage_bytes'] < reserve:
        raise CrosscheckError('INTERMEDIATE_DISK_RESERVE_BREACH')
    base = assess_archive(source, operator_path)
    # The historical source is not upgraded by this crosscheck.
    eligible = base.get('status') in (
        'HOST_REPORTED_60S_AND_DUAL_REPLAY_CANDIDATE',
        'HOST_REPORTED_24H_AND_DUAL_REPLAY_CANDIDATE')
    status = ('HOST_MONITORED_INTERNAL_SOURCE_CANDIDATE' if eligible else
              'BLOCKED_BASE_SOURCE_GATE')
    return {'schema': 'v26_capture_crosscheck_v1', 'status': status,
            'source_manifest_sha256': base.get('source_manifest_sha256'),
            'host_clock_trace': clock,
            'prior_internal_admission': base,
            'candidate_host_monitored_source': eligible,
            'independent_exchange_truth_verified': False,
            'historical_feature_availability_verified': False,
            'real_24h_verified_by_this_environment': False,
            'executable_alpha_verified': False,
            'real_orders_sent': 0}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('source', type=Path)
    ap.add_argument('evidence_directory', type=Path)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    if args.output.exists() or args.output.is_symlink():
        ap.error('Output exists')
    try:
        result = crosscheck(args.source, args.evidence_directory)
    except (ValueError, KeyError, OSError, TypeError, OverflowError) as exc:
        result = {'schema': 'v26_capture_crosscheck_v1',
                  'status': 'BLOCKED_SOURCE_OR_TEMPORAL_EVIDENCE',
                  'reason': str(exc)[:400],
                  'candidate_host_monitored_source': False,
                  'independent_exchange_truth_verified': False,
                  'executable_alpha_verified': False,
                  'real_orders_sent': 0}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf8') as target:
        json.dump(result, target, indent=2, sort_keys=True)
        target.write('\n')
    print(result['status'])
    if not result['candidate_host_monitored_source']:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
