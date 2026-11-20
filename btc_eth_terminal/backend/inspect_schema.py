"""Inspect a local historical features CSV(.gz) schema without loading trading signals.

Shows headers, row count and time-like candidates only. Never guesses OHLCV mappings.
"""
import argparse
import csv
import gzip
import json
from pathlib import Path


def inspect(src: Path) -> dict:
    if not src.is_file():
        raise FileNotFoundError(src)
    read = gzip.open if src.name.endswith('.gz') else open
    with read(src, 'rt', encoding='utf-8-sig', newline='') as fp:
        reader = csv.DictReader(fp, strict=True)
        names = reader.fieldnames or []
        n = sum(1 for _ in reader)
    return {'source': str(src), 'rows': n, 'columns': names,
            'time_candidates_not_auto_selected': [x for x in names if any(k in x.lower() for k in ('time','timestamp','date'))],
            'warning': 'No column is inferred as a valid trade or complete OHLCV bar.'}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('input', type=Path)
    a = p.parse_args()
    print(json.dumps(inspect(a.input), indent=2, ensure_ascii=False))
