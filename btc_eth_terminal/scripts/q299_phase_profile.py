"""Q299-A: ephemeral observer phase profiling; no market payloads in receipts."""
from __future__ import annotations

import atexit
import functools
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import q292_dual_gateway as gateway
import v36_live_engine as live
from v23_causal_microstructure import Book

STATS = {}
LOCK = threading.Lock()
OUT = None
LAST_FLUSH = 0.0


def record(name, elapsed):
    with LOCK:
        row = STATS.setdefault(name, [0, 0, 0])
        row[0] += 1
        row[1] += elapsed
        row[2] = max(row[2], elapsed)


def flush(force=False):
    global LAST_FLUSH
    now = time.monotonic()
    with LOCK:
        if OUT is None or not force and now - LAST_FLUSH < .5:
            return
        LAST_FLUSH = now
        result = {k: {"calls": n, "total_ns": total, "max_ns": peak}
                  for k, (n, total, peak) in STATS.items()}
        tmp = OUT.with_suffix(".tmp")
        with tmp.open("w", encoding="utf8") as stream:
            json.dump(result, stream, sort_keys=True)
        os.replace(tmp, OUT)


def timed(owner, attr, name, *, flush_after=False):
    original = getattr(owner, attr)

    @functools.wraps(original)
    def wrapped(*args, **kwargs):
        start = time.perf_counter_ns()
        try:
            return original(*args, **kwargs)
        finally:
            record(name, time.perf_counter_ns() - start)
            if flush_after:
                flush()

    setattr(owner, attr, wrapped)


@contextmanager
def timed_observation_lock(lock):
    start = time.perf_counter_ns()
    with lock:
        record("observation_lock_wait", time.perf_counter_ns() - start)
        yield


def install(args):
    global OUT
    if not args.synthetic or not args.worker:
        raise RuntimeError("Q299_PROFILE_SYNTHETIC_WORKER_ONLY")
    OUT = Path(args.workdir) / "q299_profile.json"
    # Inclusive timings overlap. Do not add them to estimate end-to-end time.
    timed(json, "loads", "json_loads_inclusive")
    timed(gateway.CheckedEngine, "_shadow_apply", "shadow_apply")
    timed(gateway.CheckedEngine, "consume", "checked_consume", flush_after=True)
    timed(live.LiveMarketEngine, "_consume", "primary_consume")
    timed(Book, "apply", "book_apply")
    timed(live, "_features", "primary_features")
    timed(gateway.CheckedEngine, "_matches_row", "independent_top10")
    timed(gateway.CheckedEngine, "checked_pair", "checked_pair")
    timed(gateway.Collector, "observation", "collector_observation", flush_after=True)
    atexit.register(flush, True)
