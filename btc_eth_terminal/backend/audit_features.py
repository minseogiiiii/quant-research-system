"""Read-only, streaming audit of BTC/ETH five-minute CSV and CSV.GZ research inputs.

This tool inspects supplied bytes, NOT the truth of the claimed exchange, fill,
execution or historical availability. It never fits a model or changes a source.
An unavailable timestamp stays unknown; missing five-minute intervals stay missing.
Python >=3.11 and standard library only.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
from pathlib import Path

from bridge import InputError, _time_ms

STEP_MS = 300_000
MAX_INPUT_BYTES = 1_000_000_000
MAX_ROWS = 2_000_000
SUSPECT_FEATURE = re.compile(
    r"(?:^|[_ .-])(future|label|labels|target|targets|forward|lookahead|lead)(?:$|[_ .-])",
    flags=re.IGNORECASE,
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fp:
        for block in iter(lambda: fp.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def audit(
    source: Path,
    *,
    time_column: str | None = None,
    available_column: str | None = None,
    venue: str | None = None,
    instrument: str | None = None,
    feature_input: bool = False,
) -> dict:
    """Inspect CSV/CSV.GZ without guessing timestamps, mappings or exchange identity."""
    source = source.resolve()
    if not source.is_file() or source.suffix.lower() not in {'.csv', '.gz'}:
        raise InputError('Source must be an existing CSV or CSV.GZ')
    if source.suffix.lower() == '.gz' and not source.name.lower().endswith('.csv.gz'):
        raise InputError('Only .csv.gz compressed inputs are supported')
    if source.stat().st_size > MAX_INPUT_BYTES:
        raise InputError('Source exceeds 1 GB audit safety limit')
    if feature_input and 'future_labels' in source.name.lower():
        raise InputError('Future-label files cannot be used as model features')
    if available_column and not time_column:
        raise InputError('Time column is required when evaluating availability')
    if venue and not venue.strip() or instrument and not instrument.strip():
        raise InputError('Declared venue/instrument must be nonempty')
    digest_before = _sha256(source)
    opener = gzip.open if source.suffix.lower() == '.gz' else open
    n = 0
    prev = -1
    gap_count = 0
    first_gaps: list[dict[str, int]] = []
    first = last = None
    observed_availability = 0
    with opener(source, 'rt', encoding='utf-8-sig', newline='') as fp:
        reader = csv.DictReader(fp, strict=True)
        fields = reader.fieldnames or []
        if not fields or len(fields) != len(set(fields)) or any(not f or not f.strip() for f in fields):
            raise InputError('Missing/duplicate/blank source CSV header')
        if time_column and time_column not in fields:
            raise InputError(f'Explicit time column does not exist: {time_column}')
        if available_column and available_column not in fields:
            raise InputError(f'Explicit availability column does not exist: {available_column}')
        suspicious = [f for f in fields if SUSPECT_FEATURE.search(f)]
        if feature_input and suspicious:
            raise InputError(f'Potential future/label columns forbidden in feature input: {suspicious}')
        for row in reader:
            n += 1
            if n > MAX_ROWS:
                raise InputError('Over 2 million rows; split into smaller bounded audits')
            if None in row or any(v is None for v in row.values()):
                raise InputError(f'Missing or extra CSV fields at row {n + 1}')
            if not time_column:
                continue
            t = _time_ms(row[time_column])
            if t % STEP_MS:
                raise InputError(f'Bar timestamp not on 5-minute UTC grid at row {n + 1}')
            if t <= prev:
                raise InputError(f'Non-increasing or duplicate bar timestamp at row {n + 1}')
            if prev >= 0:
                gap = (t - prev) // STEP_MS - 1
                if gap:
                    gap_count += gap
                    if len(first_gaps) < 10:
                        first_gaps.append({'from_open_ms': prev, 'to_open_ms': t,
                                           'missing_bars': gap})
            if available_column:
                available = _time_ms(row[available_column])
                if available < t + STEP_MS:
                    raise InputError(f'Completed OHLCV bar available before close at row {n + 1}')
                observed_availability += 1
            first = t if first is None else first
            last = t
            prev = t
    if _sha256(source) != digest_before:
        raise InputError('Source changed while audit was running')
    return {
        'input_sha256': digest_before,
        'source_format': 'csv.gz' if source.suffix.lower() == '.gz' else 'csv',
        'rows': n,
        'columns': fields,
        'suspicious_forward_label_columns': suspicious,
        'feature_input_columns_safe_by_name': not suspicious,
        'time_column_explicit': time_column,
        'available_column_explicit': available_column,
        'first_open_ms': first,
        'last_open_ms': last,
        'missing_5m_bar_intervals': gap_count if time_column else None,
        'first_missing_intervals': first_gaps,
        'time_sequence_validated': bool(time_column and n),
        'complete_regular_grid': bool(time_column and n and not gap_count),
        'availability_value_checks_passed': bool(available_column and observed_availability == n and n),
        'point_in_time_source_truth_verified': False,
        'venue_declared_not_verified': venue,
        'instrument_declared_not_verified': instrument,
        'price_and_execution_integrity_tested': False,
        'eligible_for_live_trading': False,
        'note': 'Hashes and row-level invariants do not independently prove provider authenticity, arrival time, profitability or tradability.'
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('input', type=Path)
    p.add_argument('--time-column', help='Column explicitly containing 5-minute BAR-OPEN time')
    p.add_argument('--available-column', help='Column explicitly containing independently recorded complete-bar availability')
    p.add_argument('--venue', help='Declared exchange/venue; NOT independently verified')
    p.add_argument('--instrument', help='Declared market instrument; NOT independently verified')
    p.add_argument('--feature-input', action='store_true', help='Reject source filename or column names indicating forward labels')
    a = p.parse_args()
    try:
        result = audit(a.input, time_column=a.time_column, available_column=a.available_column,
                       venue=a.venue, instrument=a.instrument, feature_input=a.feature_input)
    except (OSError, ValueError, csv.Error, EOFError) as exc:
        p.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
