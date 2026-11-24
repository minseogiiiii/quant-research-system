"""Q29.2-D: two original collector processes, independently checked books.

Candidate loopback read-only routing. No exchange orders, no continuity claim.
Both collectors share one host and Python observer failure domain in this
version; independent-host deployment is a separate acceptance requirement.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import heapq
import json
import os
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from independent_soak_replay import price, strict_json
from q25_session_isolation_guard import SourceEpochIsolatedEngine
from q292_failover_policy import Arbiter, Source
from q293_b4_diagnostics import ObserverJournal, reason_class
from q293_long_policy import DISK_RESERVE, budgets
from v36_live_engine import StateRejected
from v36_observe_existing_collector import COLLECTOR, read_tap

ROOT = Path(__file__).resolve().parents[1]
PRODUCTS = ("BTC-USD", "ETH-USD")
MAX_LEVELS = 250_000
MAX_FRAME = 18_000_000


class CheckedEngine(SourceEpochIsolatedEngine):
    """Independent Decimal ladder projection, sharing only the archived input."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.shadow = {}
        self.shadow_epoch = None
        self.shadow_seq = -1
        self.witnessed = {}
        self.witness_failure = False
        self.diagnostic = None

    def _invalid(self, reason, phase="QUARANTINED"):
        # Invalidation must happen even if the diagnostic disk fails.
        try:
            if self.diagnostic is not None:
                self.diagnostic.record(reason, self.connection_id, self.seq)
        finally:
            super()._invalid(reason, phase)
            self.witnessed.clear()

    def _shadow_apply(self, line):
        if not 0 < len(line) <= MAX_FRAME:
            raise ValueError("SHADOW_FRAME_SIZE")
        envelope = strict_json(line)
        raw = envelope["raw_text"]
        if hashlib.sha256(raw.encode()).hexdigest() != envelope["sha256"]:
            raise ValueError("SHADOW_RAW_HASH")
        wire = strict_json(raw)
        epoch = envelope["connection_id"]
        seq = wire["sequence_num"]
        if type(epoch) is not int or type(seq) is not int:
            raise ValueError("SHADOW_SEQUENCE_TYPE")
        if epoch != self.shadow_epoch:
            if seq != 0 or epoch != (
                0 if self.shadow_epoch is None else self.shadow_epoch + 1
            ):
                raise ValueError("SHADOW_EPOCH_ORDER")
            self.shadow_epoch, self.shadow_seq = epoch, -1
            self.shadow = {}
            self.witnessed.clear()
        if seq != self.shadow_seq + 1:
            raise ValueError("SHADOW_SEQUENCE_GAP")
        self.shadow_seq = seq
        if wire["channel"] != "l2_data":
            return {}
        touched = {}
        for event in wire["events"]:
            p, kind = event["product_id"], event["type"]
            if p not in PRODUCTS:
                raise ValueError("SHADOW_PRODUCT")
            if kind == "snapshot" and p not in self.shadow:
                self.shadow[p] = {"bid": {}, "offer": {}}
            elif kind != "update" or p not in self.shadow:
                raise ValueError("SHADOW_SNAPSHOT_ORDER")
            for update in event["updates"]:
                side = update["side"]
                if side not in ("bid", "offer"):
                    raise ValueError("SHADOW_SIDE")
                px = price(update["price_level"])
                qty = price(update["new_quantity"], zero=True)
                levels = self.shadow[p][side]
                touched.setdefault(p, set()).add((side, px))
                if qty:
                    levels[px] = qty
                else:
                    levels.pop(px, None)
                if len(levels) > MAX_LEVELS:
                    raise ValueError("SHADOW_DEPTH_BUDGET")
        return touched

    def _matches_row(self, p, row):
        book = self.shadow[p]
        if row["session_id"] != self.session_id:
            return False
        if row["l2_book_levels"] != {k: len(v) for k, v in book.items()}:
            return False
        for side, key in (("bid", "bids"), ("offer", "asks")):
            select_levels = heapq.nlargest if side == "bid" else heapq.nsmallest
            levels = select_levels(10, book[side].items())
            reported = row["depth"][key]
            if len(levels) != 10 or len(reported) != 10:
                return False
            running = Decimal(0)
            for (px, qty), record in zip(levels, reported, strict=True):
                running += qty
                if (px != Decimal(record["price"])
                        or qty != Decimal(record["quantity"])
                        or running != Decimal(record["cumulative_base"])):
                    return False
            total_key = "bid_total_base_10" if side == "bid" else "ask_total_base_10"
            if running != Decimal(row["depth"][total_key]):
                return False
            prefix = "best_bid" if side == "bid" else "best_ask"
            if (levels[0][0] != Decimal(row[prefix])
                    or levels[0][1] != Decimal(row[prefix + "_qty"])):
                return False
        return Decimal(row["best_bid"]) < Decimal(row["best_ask"])

    def consume(self, line):
        with self._cv:
            try:
                touched = self._shadow_apply(line)
                super().consume(line)
                # Frozen Book heaps retain duplicate entries for repeated price
                # updates. Compact without changing the exact ladder or best.
                for book in self.books.values():
                    for side, levels in book.level.items():
                        if len(book.heap[side]) > 3 * len(levels) + 1024:
                            book.heap[side] = [
                                -px if side == "bid" else px for px in levels]
                            heapq.heapify(book.heap[side])
                if self.status != "HEALTHY":
                    return
                for p in PRODUCTS:
                    book = self.shadow.get(p)
                    primary = self.books[p].level
                    if book is None or any(len(primary[k]) != len(book[k])
                                           for k in ("bid", "offer")):
                        raise ValueError("INDEPENDENT_BOOK_DIVERGENCE")
                    # Compare every modified level even between publications;
                    # retain full equality at each new publishable state.
                    if any(primary[k].get(px) != book[k].get(px)
                           for k, px in touched.get(p, ())):
                        raise ValueError("INDEPENDENT_BOOK_DIVERGENCE")
                    if not self.rows[p]:
                        continue
                    row = self.rows[p][-1]
                    if self.witnessed.get(p) == row["state_version"]:
                        continue
                    if primary != book:
                        raise ValueError("INDEPENDENT_BOOK_DIVERGENCE")
                    # Some multi-event frames publish intermediate states;
                    # do not attest them unless the independent final ladder
                    # matches. A subsequent verified state can be admitted.
                    if self._matches_row(p, row):
                        self.witnessed[p] = row["state_version"]
            except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
                self.witness_failure = True
                self._invalid("INDEPENDENT_SOURCE_WITNESS_FAILED")
                raise StateRejected("INDEPENDENT_SOURCE_WITNESS_FAILED") from exc

    def checked_pair(self):
        with self._cv:
            health = self.health()
            if health["status"] != "HEALTHY" or self.witness_failure:
                return None
            pair = {p: self.snapshot(p)["state"] for p in PRODUCTS}
            if any(row is None or self.witnessed.get(p) != row["state_version"]
                   or row["session_id"] != health["session_id"]
                   for p, row in pair.items()):
                return None
            return health, copy.deepcopy(pair)


class Collector:
    """A real original Node collector, with its own IPC and archive directory."""

    def __init__(self, name, work, duration, synthetic, *, parent_bound=False):
        self.name = name
        self.work = work
        self.synthetic = synthetic
        self.provenance = secrets.token_hex(32)
        self.process = self.thread = self.listener = None
        self.logs = []
        self.done = [False]
        self.cached = None
        self.diagnostic = None
        self.last_reason = "NO_OBSERVATION"
        self.engine = CheckedEngine(
            mode="ARCHIVE_SIMULATION" if synthetic else "LIVE_SOURCE",
            collector_runtime_verified=not synthetic,
            collector_script_sha256=hashlib.sha256(COLLECTOR.read_bytes()).hexdigest(),
        )
        try:
            work.mkdir(mode=0o700)
            self.diagnostic = ObserverJournal(work / "observer_events.jsonl")
            self.engine.diagnostic = self.diagnostic
            tap = work / "tap.sock"
            if len(os.fsencode(tap)) > 100:
                raise ValueError("DUAL_IPC_PATH_LENGTH")
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener.bind(str(tap))
            os.chmod(tap, 0o600)
            self.listener.listen(1)
            self.listener.settimeout(.25)
            raw_budget, archive_budget = budgets(duration)
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "SOAK_DURATION_SECONDS": str(duration),
                "SOAK_RAW_BUDGET_BYTES": str(raw_budget),
                "SOAK_ARCHIVE_BUDGET_BYTES": str(archive_budget),
                "SOAK_DISK_RESERVE_BYTES": str(DISK_RESERVE),
                "SOAK_CHUNK_LIMIT_BYTES": "16000" if synthetic else "8000000",
                "SOAK_TEST_FIXTURE": "1" if synthetic else "0",
                "SOAK_TEST_DISCONNECT_AFTER_MESSAGES": "0",
                "V36_LIVE_TAP_SOCKET": str(tap),
                "Q24_WS_DIAG_PRIVATE_PATH": str(work / "ws.jsonl"),
                "Q27_HEARTBEAT_GUARD_ENABLED": "1",
                "Q27_HEARTBEAT_TRACE_PRIVATE": str(work / "hb.jsonl"),
            }
            node = shutil.which("node")
            if node is None:
                raise ValueError("DUAL_NODE_REQUIRED")
            args = [node]
            if synthetic:
                env["V36_TAP_FIXTURE_MODE"] = "1"
                args += ["--import", str(ROOT / "tests/q292_dual_source_mock.mjs")]
            for preload in ("q27_heartbeat_guard.mjs", "q24_ws_close_probe.mjs"):
                args += ["--import", str(ROOT / "scripts" / preload)]
            args.append(str(COLLECTOR))
            if parent_bound:
                import sys

                args = [
                    sys.executable, str(ROOT / "scripts/q292_parent_bound_exec.py"),
                    str(os.getpid()), *args,
                ]
            for name in ("stdout.log", "stderr.log"):
                self.logs.append((work / name).open("xb"))
            self.process = subprocess.Popen(
                args, cwd=work, env=env, stdout=self.logs[0],
                stderr=self.logs[1], start_new_session=True,
            )
            self.thread = threading.Thread(target=self._receive, daemon=True)
            self.thread.start()
        except BaseException:
            self.close()
            raise

    def _receive(self):
        try:
            while not self.done[0] and self.process.poll() is None:
                try:
                    client, _ = self.listener.accept()
                except TimeoutError:
                    continue
                peer = client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                pid, uid, _gid = struct.unpack("3i", peer)
                if (pid, uid) != (self.process.pid, os.getuid()):
                    client.close()
                    self.engine.transport_lost("DUAL_UNTRUSTED_IPC")
                    continue
                read_tap(client, self.engine, self.done)
                return
        except OSError:
            self.engine.transport_lost("DUAL_IPC_FAILED")

    def observation(self):
        if self.process.poll() is not None:
            self.last_reason = "COLLECTOR_EXITED"
            return None
        with self.engine._cv:
            checked = self.engine.checked_pair()
            if checked is None:
                self.cached = None
                self.last_reason = (reason_class(self.engine.reason)
                                    if self.engine.status != "HEALTHY"
                                    else "BOOK_WITNESS_NOT_READY")
            else:
                self.last_reason = "READY"
            self.diagnostic.record(self.last_reason, self.engine.connection_id,
                                   self.engine.seq)
        if checked is None:
            return None
        health, pair = checked
        version = tuple(pair[p]["state_version"] for p in PRODUCTS)
        key = (health["session_id"], version)
        if self.cached is None or self.cached[0] != key:
            now_ns = time.monotonic_ns()
            now_ms = time.time_ns() // 1_000_000
            age = max(now_ms - pair[p]["source_last_l2_receiver_ms"]
                      for p in PRODUCTS)
            self.cached = (key, Source(
                self.name, health["session_id"], now_ns, age,
                True, True, True, True, self.provenance,
            ), pair)
        return self.cached[1:]

    def close(self):
        self.done[0] = True
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=4)
        if self.listener is not None:
            self.listener.close()
        if self.thread is not None:
            self.thread.join(timeout=3)
        try:
            self.engine.stopped()
        finally:
            if self.diagnostic is not None:
                self.engine.diagnostic = None
                self.diagnostic.close()
            for stream in self.logs:
                stream.close()


class DualRoute:
    def __init__(self, primary, secondary):
        self.sources = (primary, secondary)
        self.arbiter = Arbiter()
        self.lock = threading.Lock()
        self.run = secrets.token_hex(16)

    def read(self):
        with self.lock:
            observations = [s.observation() for s in self.sources]
            candidates = [x[0] if x is not None else None for x in observations]
            # Different receive times do not establish comparable exchange
            # checkpoints. Never merge books or infer a gap is repaired.
            route = self.arbiter.update(time.monotonic_ns(), *candidates)
            name = route["active_source"]
            pair = None
            if name:
                index = 0 if name == "primary" else 1
                pair = observations[index][1]
                # Recheck child lifetime at the response linearization point.
                if self.sources[index].process.poll() is not None:
                    route = self.arbiter.update(
                        time.monotonic_ns(), disagreement=True)
                    pair = None
            return {
                **route, "schema": "q292_dual_route_v1",
                "gateway_run": self.run,
                "mode": "SYNTHETIC_ONLY" if self.sources[0].synthetic
                else "LIVE_READ_ONLY_CANDIDATE",
                "states": pair,
                "exchange_attested": False,
                "execution_authorized": False,
            }


def cursor(body):
    return (f'{body["gateway_run"]}:{body["generation"]}:'
            f'{body["source_session"]}')


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(2)
        super().setup()

    def log_message(self, *_args):
        pass

    def reply(self, code, body, mime="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(
            body, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def do_GET(self):
        if self.path not in ("/snapshot", "/events"):
            self.reply(404, {"error": "UNKNOWN_READ_ONLY_ROUTE"})
            return
        body = self.server.route.read()
        last = self.headers.get("Last-Event-ID")
        current = cursor(body)
        if last is not None and (last != current or body["states"] is None):
            body = {**body, "states": None, "status": "RESYNC_REQUIRED"}
            if self.path == "/snapshot":
                self.reply(409, body)
                return
        if self.path == "/events":
            # One bounded SSE event per connection, no stale buffering or
            # replay across generation; clients reconnect for fresh snapshots.
            event = "snapshot" if body["states"] is not None else "resync_required"
            data = json.dumps(body, separators=(",", ":"), allow_nan=False)
            payload = f"id: {current}\nevent: {event}\ndata: {data}\n\n".encode()
            self.reply(200, payload, "text/event-stream")
        else:
            self.reply(200 if body["states"] is not None else 503, body)

    def do_POST(self):
        self.reply(405, {"error": "READ_ONLY", "real_orders_enabled": False})

    do_PUT = do_DELETE = do_PATCH = do_POST


def server_for_dual(route, port=0):
    server = HTTPServer(("127.0.0.1", port), Handler)
    server.route = route
    return server


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workdir", type=Path, required=True)
    p.add_argument("--port", type=int, default=8789)
    p.add_argument("--duration", type=int, default=180)
    modes = p.add_mutually_exclusive_group(required=True)
    modes.add_argument("--synthetic", action="store_true")
    modes.add_argument("--execute-public-pilot", action="store_true")
    args = p.parse_args()
    if (not 15 <= args.duration <= 180
            or args.execute_public_pilot and args.duration != 180):
        p.error("only bounded candidate pilot is permitted")
    if shutil.disk_usage(args.workdir.parent).free < 3 * 1024**3:
        p.error("insufficient dual collector disk budget")
    args.workdir.mkdir(mode=0o700)
    sources = []
    stop = threading.Event()

    def halt(_sig, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, halt)
    signal.signal(signal.SIGINT, halt)
    try:
        for name in ("primary", "secondary"):
            sources.append(Collector(name, args.workdir / name,
                                     args.duration, args.synthetic))
        route = DualRoute(*sources)
        with server_for_dual(route, args.port) as server:
            server.timeout = .2
            end = time.monotonic() + args.duration
            while not stop.is_set() and time.monotonic() < end:
                route.read()
                server.handle_request()
    finally:
        for source in sources:
            with contextlib.suppress(OSError):
                source.close()


if __name__ == "__main__":
    main()
