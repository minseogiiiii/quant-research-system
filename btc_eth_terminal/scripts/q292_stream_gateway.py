"""Q29.2-F: durable route transitions and bounded persistent snapshot SSE.

Candidate only. No gapless replay, venue attestation, or order admission.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from q292_dual_gateway import Handler, cursor
from q292_incident_journal import canonical

MAX_RECORDS = 10_000
MAX_LINE = 2048
ZERO = "0" * 64
TTL_NS = 3_000_000_000


def record_fields(body):
    session = body.get("source_session")
    source = body.get("active_source")
    if source not in (None, "primary", "secondary"):
        raise ValueError("LEDGER_SOURCE")
    if (source is None) != (session is None):
        raise ValueError("LEDGER_SESSION")
    if session is not None and not re.fullmatch("[0-9a-f]{32}", session):
        raise ValueError("LEDGER_SESSION")
    generation = body["generation"]
    run = body["gateway_run"]
    if (type(generation) is not int or generation < 0
            or not re.fullmatch("[0-9a-f]{32}", run)):
        raise ValueError("LEDGER_GENERATION")
    return {"run": run, "generation": generation, "source": source,
            "session_sha256": hashlib.sha256(session.encode()).hexdigest()
            if session else None,
            "real_orders_enabled": False, "historical_gap_recovered": False}


class TransitionLedger:
    """Fresh exclusive file per gateway run. No trust in a torn tail."""

    def __init__(self, path):
        path = Path(path)
        if path.parent.stat().st_mode & 0o077:
            raise ValueError("LEDGER_PRIVATE_DIRECTORY_REQUIRED")
        self.fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                          os.O_NOFOLLOW, 0o600)
        self.last = None
        self.digest = ZERO
        self.count = 0
        self.failed = False
        try:
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            os.close(self.fd)
            raise

    def commit(self, body):
        if self.failed:
            raise ValueError("LEDGER_FAILED")
        value = record_fields(body)
        if value == self.last:
            return
        if self.last and (value["run"] != self.last["run"]
                          or value["generation"] <= self.last["generation"]):
            raise ValueError("LEDGER_NONMONOTONE_TRANSITION")
        row = {"index": self.count, "previous": self.digest, **value}
        digest = hashlib.sha256(canonical(row)).hexdigest()
        raw = canonical({**row, "sha256": digest}) + b"\n"
        if self.count >= MAX_RECORDS or len(raw) > MAX_LINE:
            raise ValueError("LEDGER_BUDGET")
        try:
            view = memoryview(raw)
            while view:
                n = os.write(self.fd, view)
                if n <= 0:
                    raise OSError("LEDGER_SHORT_WRITE")
                view = view[n:]
            os.fsync(self.fd)
        except OSError:
            self.failed = True
            raise
        self.count += 1
        self.digest = digest
        self.last = value

    def close(self):
        os.close(self.fd)


def audit_ledger(path):
    """Verify committed prefix, distinguish torn tail from valid completion."""
    previous, count, generation, run = ZERO, 0, -1, None
    torn = False
    with Path(path).open("rb") as stream:
        while raw := stream.readline(MAX_LINE + 1):
            if len(raw) > MAX_LINE or count >= MAX_RECORDS:
                raise ValueError("LEDGER_BOUND")
            if not raw.endswith(b"\n"):
                torn = True
                break
            row = json.loads(raw)
            digest = row.pop("sha256")
            if (set(row) != {"index", "previous", "run", "generation",
                             "source", "session_sha256", "real_orders_enabled",
                             "historical_gap_recovered"}
                    or row["index"] != count or row["previous"] != previous
                    or row["generation"] <= generation
                    or run is not None and row["run"] != run
                    or row["real_orders_enabled"] is not False
                    or row["historical_gap_recovered"] is not False
                    or hashlib.sha256(canonical(row)).hexdigest() != digest):
                raise ValueError("LEDGER_INTEGRITY")
            count += 1
            previous, generation, run = digest, row["generation"], row["run"]
    return {"records": count, "torn_tail": torn, "terminal_sha256": previous,
            "zero_gap_certified": False}


def unavailable(body):
    return {**body, "states": None, "status": "QUARANTINED",
            "active_source": None, "source_session": None}


class Publisher:
    """Single poll writer; requests never perform blocking observer IPC."""

    def __init__(self, route, ledger):
        self.route = route
        self.ledger = ledger
        self.lock = threading.Lock()
        self.body = None
        self.published_ns = 0
        self.failure = False
        self.revoked_generation = -1
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.thread.start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                with self.lock:
                    revoked = self.revoked_generation
                if revoked >= 0 and hasattr(self.route, "invalidate"):
                    self.route.invalidate(revoked)
                body = self.route.read()
                # Persist transition before exposing its state to any client.
                self.ledger.commit(body)
                with self.lock:
                    if body["generation"] <= self.revoked_generation:
                        body = unavailable(body)
                    self.body = body
                    self.published_ns = time.monotonic_ns()
            except (OSError, ValueError, TypeError, KeyError):
                with self.lock:
                    self.failure = True
                    if self.body is not None:
                        self.body = unavailable(self.body)
                return
            self.stop.wait(.15)

    def read(self):
        with self.lock:
            if self.body is None:
                return {"gateway_run": self.route.run, "generation": 0,
                        "source_session": None, "active_source": None,
                        "states": None, "status": "QUARANTINED",
                        "real_orders_enabled": False, "zero_gap_certified": False}
            body = self.body
            states = body["states"]
            expired = time.monotonic_ns() - self.published_ns > TTL_NS
            if states is not None:
                now = time.time_ns() // 1_000_000
                expired = expired or any(
                    not 0 <= now - row["source_last_l2_receiver_ms"] <= 3000
                    for row in states.values())
            if self.failure or expired:
                # Do not let an expired public generation resurrect after a
                # delayed poll. A fresh arbiter generation is required.
                self.revoked_generation = max(
                    self.revoked_generation, body["generation"])
                return unavailable(body)
            return body

    def close(self):
        self.stop.set()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("PUBLISHER_STOP_TIMEOUT")


class StreamHandler(Handler):
    def do_GET(self):
        if self.path != "/events":
            super().do_GET()
            return
        if not self.server.stream_slots.acquire(blocking=False):
            self.reply(503, {"error": "SSE_CAPACITY"})
            return
        try:
            self.stream()
        except (OSError, TimeoutError):
            pass
        finally:
            self.close_connection = True
            self.server.stream_slots.release()

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        bound = self.headers.get("Last-Event-ID")
        previous = None
        deadline = time.monotonic() + self.server.stream_seconds
        while time.monotonic() < deadline and not self.server.stopping.is_set():
            body = self.server.route.read()
            current = cursor(body)
            invalid = body["states"] is None or bound not in (None, current)
            if invalid:
                body = {**body, "states": None, "status": "RESYNC_REQUIRED"}
                event = "resync_required"
            else:
                event = "snapshot"
                bound = current
            data = json.dumps(body, separators=(",", ":"), allow_nan=False)
            if data != previous or invalid:
                raw = f"id: {current}\nevent: {event}\ndata: {data}\n\n"
                self.wfile.write(raw.encode())
                self.wfile.flush()
                previous = data
            else:
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
            if invalid:
                return
            self.server.stopping.wait(.25)


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, publisher, port=0, stream_seconds=120):
        self.route = publisher
        self.stream_seconds = stream_seconds
        self.stream_slots = threading.BoundedSemaphore(8)
        self.workers = threading.BoundedSemaphore(12)
        self.stopping = threading.Event()
        super().__init__(("127.0.0.1", port), StreamHandler)

    def process_request(self, request, address):
        if not self.workers.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.workers.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.workers.release()

    def server_close(self):
        self.stopping.set()
        super().server_close()
