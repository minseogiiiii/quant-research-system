"""Measure existing local read-only BTC/ETH pipeline before considering new dependencies.

No provider calls, no simulated market observations, no changes to input files.
A real dataset must be registered to obtain market-data workload timings.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
import tracemalloc

import bridge


def benchmark(rounds: int = 7) -> dict:
    if not 1 <= rounds <= 100:
        raise bridge.InputError('Rounds must be between 1 and 100')
    doc = bridge.read_manifest()
    ds = [x for x in doc['datasets'] if x['type'] == 'crypto' and x['resolution'] == '5']
    timings: list[float] = []
    peak_bytes = 0
    if not ds:
        return {'status': 'NO_REGISTERED_MARKET_DATA', 'timings_ms': [],
                'performance_bottleneck_demonstrated': False,
                'note': 'No A/B integration decision is supported by an empty data manifest'}
    for _ in range(rounds):
        tracemalloc.start()
        start = time.perf_counter_ns()
        for item in ds:
            data = bridge.get_bars(doc, item['symbol'], item['resolution'],
                                   7_258_118_400_000, 0, bridge.MAX_REQUEST_BARS)
            if not data['bars']:
                raise bridge.InputError('Registered dataset produced no bars')
        timings.append((time.perf_counter_ns() - start) / 1_000_000)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        peak_bytes = max(peak_bytes, peak)
    return {'status': 'LOCAL_READ_ONLY_MEASUREMENT',
            'dataset_symbols': [d['symbol'] for d in ds],
            'rounds': rounds, 'median_ms': statistics.median(timings),
            'max_ms': max(timings), 'tracemalloc_peak_bytes': peak_bytes,
            'method': 'In-process Python parse + provenance verification + history filtering; no network; not event latency',
            'performance_bottleneck_demonstrated': False,
            'note': 'Requires independent representative A/B baseline before adopting CSP, ArcticDB or C++.'}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--rounds', type=int, default=7)
    args = p.parse_args()
    print(json.dumps(benchmark(args.rounds), indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
