"""Q29.2-G: bounded re-admission, exact-input checkpoint comparison.

Same-wire comparison is diagnostic, not a claim of exchange-wide ordering.
Separate sessions are never merged. Candidate mode only, no execution.
"""
from __future__ import annotations

import argparse
import contextlib
import re
import secrets
import shutil
import signal
import threading
import time
from decimal import Decimal
from pathlib import Path

from q292_dual_gateway import PRODUCTS
from q292_failover_policy import Arbiter
from q292_isolated_observer import ObserverProcess
from q292_stream_gateway import BoundedServer, Publisher, TransitionLedger

MAX_RESTARTS = 3
STARTUP_LIMIT = 12
ABSENCE_LIMIT = 8


def ladder(row):
    """Exact Decimal representation, independent of string formatting."""
    result = []
    for side in ("bids", "asks"):
        levels = row["depth"][side]
        if len(levels) != 10:
            raise ValueError("CHECKPOINT_DEPTH")
        pairs = tuple((Decimal(x["price"]), Decimal(x["quantity"]))
                      for x in levels)
        if any(not p.is_finite() or not q.is_finite() or p <= 0 or q <= 0
               for p, q in pairs):
            raise ValueError("CHECKPOINT_DECIMAL")
        result.append(pairs)
    return tuple(result)


def compare(a, b):
    """Only compare equal raw L2 payload hashes, never asynchronous prices.

    Equal hashes bind to the same observed raw input, not necessarily an
    exchange-signed common checkpoint. Divergence is conservatively isolated.
    """
    report = {}
    for product in PRODUCTS:
        if a is None or b is None:
            report[product] = "SOURCE_UNAVAILABLE"
            continue
        x, y = a[1][product], b[1][product]
        hx, hy = x.get("source_raw_sha256"), y.get("source_raw_sha256")
        if (type(hx) is not str or not re.fullmatch("[0-9a-f]{64}", hx)
                or hx != hy):
            report[product] = "NOT_COMPARABLE"
            continue
        report[product] = ("MATCH" if ladder(x) == ladder(y)
                           else "DIVERGENCE")
    return report


class ManagedSource:
    """Restart isolated observer into a unique archive, never reuse IPC."""

    def __init__(self, name, root, duration, synthetic, factory=ObserverProcess):
        self.name, self.root, self.duration = name, Path(root), duration
        self.synthetic, self.factory = synthetic, factory
        self.root.mkdir(mode=0o700)
        self.worker = None
        self.attempts = 0
        self.next_start = 0.0
        self.last_good = None
        self.started = 0.0
        self.seen_ready = False
        self.closed = False
        self.retiring = False

    def retire(self):
        if self.worker is not None and not self.retiring:
            self.retiring = True
            if self.worker.process.poll() is None:
                self.worker.process.kill()

    def tick(self):
        if self.closed:
            return None
        now = time.monotonic()
        if self.worker is not None:
            if self.worker.process.poll() is not None:
                self.worker.close()
                self.worker = None
                self.retiring = False
                self.next_start = now + min(2 ** (self.attempts - 1), 8)
            elif self.retiring:
                return None
        if self.worker is None:
            if self.attempts > MAX_RESTARTS or now < self.next_start:
                return None
            self.attempts += 1
            self.started = now
            self.last_good = None
            self.seen_ready = False
            try:
                self.worker = self.factory(
                    self.name, self.root / f"attempt_{self.attempts:02d}",
                    self.duration, self.synthetic)
            except (OSError, ValueError):
                self.next_start = now + min(2 ** (self.attempts - 1), 8)
                return None
        value = self.worker.observation()
        if value is not None:
            self.seen_ready = True
            self.last_good = now
            return value
        if (self.worker.failed
                or not self.seen_ready and now - self.started > STARTUP_LIMIT
                or self.last_good is not None
                and now - self.last_good > ABSENCE_LIMIT):
            self.retire()
        return None

    def alive(self):
        return (self.worker is not None and not self.retiring
                and not self.worker.failed
                and self.worker.process.poll() is None)

    def close(self):
        self.closed = True
        if self.worker is not None:
            self.worker.close()


class ResilientRoute:
    """Single poll owner; sticky active source; re-admission needs fresh proof."""

    def __init__(self, primary, secondary):
        if primary.synthetic != secondary.synthetic:
            raise ValueError("MIXED_SOURCE_MODE")
        self.sources = (primary, secondary)
        self.run = secrets.token_hex(16)
        self.arbiter = Arbiter()
        self.last_invalidation = -1

    def invalidate(self, generation):
        if generation <= self.last_invalidation:
            return
        self.last_invalidation = generation
        if self.arbiter.generation == generation and self.arbiter.active:
            label = self.arbiter.active[0]
            self.sources[0 if label == "primary" else 1].retire()
            self.arbiter.update(time.monotonic_ns(), disagreement=True)

    def read(self):
        observations = [source.tick() for source in self.sources]
        checks = compare(*observations)
        divergent = "DIVERGENCE" in checks.values()
        before = self.arbiter.active
        candidates = [v[0] if v is not None else None for v in observations]
        route = self.arbiter.update(
            time.monotonic_ns(), *candidates, disagreement=divergent)
        if divergent:
            for source in self.sources:
                source.retire()
        elif before and route["revoked"]:
            index = 0 if before[0] == "primary" else 1
            # Do not restart a healthy new epoch already observed on this
            # source; only retire when its previous session is still present.
            value = observations[index]
            if value is not None and value[0].session == before[1]:
                self.sources[index].retire()
        pair = None
        if route["active_source"] is not None:
            index = 0 if route["active_source"] == "primary" else 1
            if self.sources[index].alive():
                pair = observations[index][1]
            else:
                route = self.arbiter.update(
                    time.monotonic_ns(), disagreement=True)
        return {
            **route, "gateway_run": self.run, "schema": "q292_resilient_route_v1",
            "mode": "SYNTHETIC_ONLY" if self.sources[0].synthetic
            else "LIVE_READ_ONLY_CANDIDATE",
            "states": pair, "checkpoint_comparison": checks,
            "source_attempts": {s.name: s.attempts for s in self.sources},
            "exchange_attested": False, "execution_authorized": False,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--duration", type=int, default=180)
    parser.add_argument("--port", type=int, default=8791)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--synthetic", action="store_true")
    mode.add_argument("--execute-public-pilot", action="store_true")
    args = parser.parse_args()
    if not 15 <= args.duration <= 180:
        parser.error("bounded candidate pilot only")
    if args.execute_public_pilot and args.duration != 180:
        parser.error("public pilot requires exactly 180s")
    if shutil.disk_usage(args.workdir.parent).free < 5 * 1024**3:
        parser.error("insufficient bounded archive budget")
    args.workdir.mkdir(mode=0o700)
    sources = []
    ledger = publisher = server = None
    thread = None
    stop = threading.Event()

    def halt(_sig, _frame):
        stop.set()

    signal.signal(signal.SIGINT, halt)
    signal.signal(signal.SIGTERM, halt)
    try:
        for name in ("primary", "secondary"):
            sources.append(ManagedSource(name, args.workdir / name,
                                         args.duration, args.synthetic))
        route = ResilientRoute(*sources)
        ledger = TransitionLedger(args.workdir / "transitions.jsonl")
        publisher = Publisher(route, ledger)
        publisher.start()
        server = BoundedServer(publisher, args.port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stop.wait(args.duration)
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=3)
        try:
            if publisher is not None:
                publisher.close()
        finally:
            for source in sources:
                with contextlib.suppress(OSError):
                    source.close()
            if ledger is not None:
                ledger.close()


if __name__ == "__main__":
    main()
