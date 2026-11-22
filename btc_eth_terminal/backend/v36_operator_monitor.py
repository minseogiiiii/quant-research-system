"""Private read-only health and transport metrics independent of HTTP/SSE slots.

The main v36 browser listener has a finite connection budget. Two incomplete
HTTP headers can occupy its remaining slots after eight persistent SSE clients
have subscribed. This Unix-only control listener keeps operational health
queries available without changing the exchange collector or browser API.

Access relies on a fresh operator workdir (mode 0700) and AF_UNIX socket mode
0600. It is not a substitute for OS user isolation, disk monitoring, or an
independent external watchdog. The listener exposes no market writes.
"""

from __future__ import annotations

import io
import json
import os
import select
import sys
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn, UnixStreamServer
from threading import BoundedSemaphore

from v36_live_engine import LiveMarketEngine

ROUTES = frozenset(("/api/v36/health", "/api/v36/metrics"))
MAX_OPERATOR_WORKERS = 12
MAX_OPERATOR_HEADER_BYTES = 8192
OPERATOR_HEADER_DEADLINE_SECONDS = 0.75


def admitted_live(health: dict) -> bool:
    return (
        health.get("mode") == "LIVE_SOURCE"
        and health.get("live_feed") is True
        and health.get("status") == "HEALTHY"
        and health.get("trading_enabled") is False
    )


class OperatorHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "V36OperatorMonitor"
    sys_version = ""

    def setup(self):
        self.request.settimeout(1.0)
        super().setup()

    def handle(self):
        """Read a single small request header by a wall-clock deadline.

        socket timeouts alone do not prevent slow-drip headers from holding a
        worker indefinitely. Parse the bounded header with the standard HTTP
        parser only once it is complete. Never permit HTTP keep-alive here.
        """
        deadline = time.monotonic() + OPERATOR_HEADER_DEADLINE_SECONDS
        header = bytearray()
        while b"\r\n\r\n" not in header:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or len(header) >= MAX_OPERATOR_HEADER_BYTES:
                return  # Fail closed without publishing a success response.
            readable, _, _ = select.select([self.connection], [], [], remaining)
            if not readable:
                return
            try:
                piece = self.connection.recv(
                    MAX_OPERATOR_HEADER_BYTES + 1 - len(header)
                )
            except (OSError, TimeoutError):
                return
            if not piece:
                return
            header.extend(piece)
            if len(header) > MAX_OPERATOR_HEADER_BYTES:
                return
        # The built-in parser never reads from an untrusted live socket and
        # therefore cannot hang on a drip-fed header.
        self.rfile.close()
        self.rfile = io.BytesIO(bytes(header))
        self.close_connection = True
        self.handle_one_request()
        self.close_connection = True

    def log_message(self, format, *args):
        # Avoid propagating untrusted operator request paths to stdout logs.
        return

    def _send_json(self, code: int, body: dict):
        content = (
            json.dumps(body, separators=(",", ":"), allow_nan=False) + "\n"
        ).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(content)

    def do_GET(self):
        if self.path not in ROUTES:
            self._send_json(
                404, {"error": "UNKNOWN_OPERATOR_ROUTE", "trading_enabled": False}
            )
            return
        health = self.server.engine.health()
        healthy = admitted_live(health)
        if self.path == "/api/v36/health":
            self._send_json(
                200 if healthy else 503,
                {
                    **health,
                    "operator_monitor_schema": "v36_3_private_operator_monitor_v1",
                    "operationally_healthy": healthy,
                    "monitor_transport": "UNIX_SOCKET",
                },
            )
            return
        counters = self.server.gateway.metrics()
        self._send_json(
            200,
            {
                "schema": "v36_3_private_operator_metrics_v1",
                "mode": health["mode"],
                "live_feed": health["live_feed"],
                "source_status": health["status"],
                "operationally_healthy": healthy,
                "trading_enabled": False,
                "execution_authorized": False,
                "monitor_transport": "UNIX_SOCKET",
                "max_clients": counters["max_clients"],
                "max_sse_clients": counters["max_sse_clients"],
                "active_sse_connections": counters["active_sse_connections"],
                "active_connections": counters["active_connections"],
                "rejected_client_slots": counters["rejected_client_slots"],
                "sse_rejected_slots": counters["sse_rejected_slots"],
            },
        )

    def do_POST(self):
        self._send_json(405, {"error": "OPERATOR_READ_ONLY", "trading_enabled": False})

    def do_PUT(self):
        self.do_POST()

    def do_DELETE(self):
        self.do_POST()


class PrivateOperatorServer(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16

    def __init__(self, path: str, engine: LiveMarketEngine, gateway):
        self.engine = engine
        self.gateway = gateway
        self._operator_slots = BoundedSemaphore(MAX_OPERATOR_WORKERS)
        super().__init__(path, OperatorHandler)

    def process_request(self, request, client_address):
        # ThreadingMixIn otherwise starts an unbounded thread per connection.
        if not self._operator_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._operator_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._operator_slots.release()

    def handle_error(self, request, client_address):
        # Expected short-lived probe disconnects must not flood stderr.
        if isinstance(
            sys.exc_info()[1],
            (
                BrokenPipeError,
                ConnectionResetError,
                ConnectionAbortedError,
                BlockingIOError,
                TimeoutError,
            ),
        ):
            return
        super().handle_error(request, client_address)


def server_for_operator(engine: LiveMarketEngine, gateway, socket_path: Path):
    path = Path(socket_path)
    # Linux AF_UNIX sun_path capacity is usually 108 bytes including NUL.
    if len(os.fsencode(path)) > 100:
        raise ValueError("OPERATOR_SOCKET_PATH_TOO_LONG")
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise ValueError("OPERATOR_WORKDIR_MISSING_OR_SYMLINK")
    # Fail closed rather than replacing any existing socket, symlink or file.
    if path.exists() or path.is_symlink():
        raise FileExistsError("OPERATOR_SOCKET_ALREADY_EXISTS")
    old_umask = os.umask(0o177)
    try:
        server = PrivateOperatorServer(str(path), engine, gateway)
    finally:
        os.umask(old_umask)
    os.chmod(path, 0o600)
    return server
