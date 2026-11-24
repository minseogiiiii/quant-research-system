"""Isolated research qualification adapter. No live engine import or alpha claim."""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Sequence

import numpy as np

MAX_ROWS = 500_000
MAX_TRIALS = 500
YEAR_MS = 365 * 24 * 60 * 60 * 1000


def need(ok, reason):
    if not ok:
        raise ValueError(reason)


def regular_returns(values, timestamps, interval_ms):
    """Explicit complete regular grid: missing returns are not zero returns."""
    need(type(interval_ms) is int and 0 < interval_ms <= YEAR_MS,
         "F2_INTERVAL")
    need(2 <= len(timestamps) <= MAX_ROWS, "F2_ROW_BOUND")
    need(all(type(t) is int and 0 < t < 2**53 for t in timestamps),
         "F2_TIMESTAMP")
    need(all(b - a == interval_ms for a, b in zip(timestamps, timestamps[1:])),
         "F2_GRID_GAP_OR_DUPLICATE")
    r = np.asarray(values, dtype=float)
    need(r.ndim in (1, 2) and r.shape[0] == len(timestamps)
         and (r.ndim == 1 or 1 <= r.shape[1] <= MAX_TRIALS)
         and r.size <= 5_000_000, "F2_SHAPE")
    need(np.isfinite(r).all() and np.all(r > -1), "F2_INVALID_OR_MISSING_RETURN")
    return r, YEAR_MS / interval_ms


def metric_witness(values, timestamps, interval_ms):
    r, ppy = regular_returns(values, timestamps, interval_ms)
    need(r.ndim == 1, "F2_ONE_SERIES")
    n = len(r)
    # Preserve the existing engine's minimum sample gate and distinguish ddof.
    mean = float(np.mean(r))
    sample = float(np.std(r, ddof=1))
    population = float(np.std(r, ddof=0))
    return {"observations": n, "periods_per_year": ppy,
            "sample_sharpe": mean / sample * math.sqrt(ppy)
            if n >= 20 and sample > 0 else None,
            "population_sharpe": mean / population * math.sqrt(ppy)
            if n >= 20 and population > 0 else None,
            "annualization_convention": "365_DAY_CONTINUOUS_GRID",
            "short_sample_warning": n * interval_ms < 30 * 86400000,
            "alpha_confirmed": False}


@dataclass(frozen=True)
class Event:
    decision_ms: int
    feature_available_ms: int
    label_end_ms: int
    label_available_ms: int
    session: str


def chronological_split(events: Sequence[Event], start, end, embargo_ms):
    """Forward-only split. Training labels must be available before OOS starts."""
    need(type(start) is int and type(end) is int and 0 < start < end < 2**53,
         "F2_SPLIT_WINDOW")
    need(type(embargo_ms) is int and embargo_ms >= 0, "F2_EMBARGO")
    need(0 < len(events) <= MAX_ROWS, "F2_EVENT_BOUND")
    prior = -1
    train, test, purged = [], [], []
    for i, event in enumerate(events):
        need(isinstance(event, Event) and all(type(v) is int for v in (
            event.decision_ms, event.feature_available_ms,
            event.label_end_ms, event.label_available_ms)), "F2_EVENT_SCHEMA")
        need(0 < event.feature_available_ms <= event.decision_ms
             < event.label_end_ms <= event.label_available_ms < 2**53
             and event.decision_ms > prior and isinstance(event.session, str)
             and 0 < len(event.session) <= 128, "F2_CAUSAL_ORDER")
        prior = event.decision_ms
        if event.decision_ms < start:
            if event.label_available_ms < start - embargo_ms:
                train.append(i)
            else:
                purged.append(i)
        elif start <= event.decision_ms < end:
            if event.label_available_ms < end:
                test.append(i)
            else:
                purged.append(i)
    need(train and test, "F2_EMPTY_SPLIT")
    return {"train": train, "test": test, "purged": purged,
            "future_training_allowed": False}


def psr_reference(sr, hurdle, n, skew, raw_kurtosis):
    need(type(n) is int and n >= 3 and all(math.isfinite(v) for v in
         (sr, hurdle, skew, raw_kurtosis)), "F2_PSR_INPUT")
    variance = 1 - skew * sr + (raw_kurtosis - 1) * sr**2 / 4
    if variance <= 0:
        return None
    return NormalDist().cdf((sr - hurdle) * math.sqrt(n - 1) / math.sqrt(variance))


def qualified_pbo(matrix, timestamps, interval_ms, upstream):
    """Allow only complete trial matrices, not upstream NaN-to-zero coercion."""
    r, _ = regular_returns(matrix, timestamps, interval_ms)
    need(r.ndim == 2, "F2_TRIAL_MATRIX")
    need(np.all(np.std(r, axis=0) > 0), "F2_DEGENERATE_TRIAL")
    result = upstream(r)
    need(result["pbo"] is None or 0 <= result["pbo"] <= 1, "F2_PBO_RANGE")
    return result
