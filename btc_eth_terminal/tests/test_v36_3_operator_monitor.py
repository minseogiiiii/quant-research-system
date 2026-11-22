"""Regression for monitoring starvation under eight SSE and two slow headers.

SYNTHETIC fault injection. No real exchange data, Linux service or 24h claim.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from threading import Thread
from threading import enumerate as active_threads
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))
from test_v36_ci_synthetic_admission import boot
from v36_live_gateway import server_for_live
from v36_operator_monitor import (
    MAX_OPERATOR_WORKERS,
    OPERATOR_HEADER_DEADLINE_SECONDS,
    server_for_operator,
)


def private_request(path: Path, route: str, method="GET"):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        sock.connect(str(path))
        sock.sendall(
            f"{method} {route} HTTP/1.1\r\nHost: local\r\nConnection: close\r\n\r\n".encode()
        )
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
        raw = b"".join(chunks)
        header, sep, body = raw.partition(b"\r\n\r\n")
        if not sep:
            raise AssertionError("missing HTTP response headers")
        return int(header.split(b" ")[1]), json.loads(body)
    finally:
        sock.close()


class PrivateOperatorMonitor(unittest.TestCase):
    def test_monitor_independent_of_8_sse_and_2_slow_http(self):
        engine, _ = boot()
        server = server_for_live(engine)
        gateway_thread = Thread(target=server.serve_forever, daemon=True)
        gateway_thread.start()
        clients, blockers = [], []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v36_monitor.sock"
            operator = server_for_operator(engine, server, path)
            operator_thread = Thread(target=operator.serve_forever, daemon=True)
            operator_thread.start()
            try:
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
                address = f"http://127.0.0.1:{server.server_address[1]}"
                for _ in range(8):
                    response = urlopen(
                        address + "/api/v36/stream?product=BTC-USD", timeout=3
                    )
                    self.assertEqual(response.status, 200)
                    clients.append(response)
                deadline = time.monotonic() + 2
                while (
                    time.monotonic() < deadline
                    and server.metrics()["active_sse_connections"] < 8
                ):
                    time.sleep(0.02)
                self.assertEqual(server.metrics()["active_sse_connections"], 8)
                # Two clients never complete request headers. Their 3-second
                # server-side timeouts do not interfere with the Unix monitor.
                for _ in range(2):
                    s = socket.create_connection(server.server_address, timeout=2)
                    s.sendall(b"GET /api/v36/health HTTP/1.1\r\nHost: slow")
                    blockers.append(s)
                deadline = time.monotonic() + 1.5
                while (
                    time.monotonic() < deadline
                    and server.metrics()["active_connections"] < 10
                ):
                    time.sleep(0.01)
                self.assertEqual(server.metrics()["active_connections"], 10)
                # Exactly this state previously caused HTTP monitoring to fail.
                for _ in range(5):
                    code, health = private_request(path, "/api/v36/health")
                    self.assertEqual(code, 503)  # Simulated, never LIVE_SOURCE.
                    self.assertEqual(health["status"], "HEALTHY")
                    self.assertFalse(health["operationally_healthy"])
                    self.assertFalse(health["live_feed"])
                    self.assertFalse(health["trading_enabled"])
                    code, metrics = private_request(path, "/api/v36/metrics")
                    self.assertEqual(code, 200)
                    self.assertEqual(metrics["active_sse_connections"], 8)
                    self.assertEqual(metrics["max_sse_clients"], 8)
                    self.assertEqual(metrics["max_clients"], 10)
                code, _ = private_request(path, "/api/v36/orders", "POST")
                self.assertEqual(code, 405)
                code, _ = private_request(path, "/api/v36/orders")
                self.assertEqual(code, 404)
                engine.transport_lost("SYNTHETIC_UNTRUSTED_SOURCE")
                code, health = private_request(path, "/api/v36/health")
                self.assertEqual(code, 503)
                self.assertEqual(health["status"], "RECONNECTING")
                self.assertFalse(health["operationally_healthy"])
            finally:
                for client in clients:
                    client.close()
                for blocker in blockers:
                    blocker.close()
                operator.shutdown()
                operator.server_close()
                operator_thread.join(timeout=4)
                path.unlink(missing_ok=True)
        server.shutdown()
        server.server_close()
        gateway_thread.join(timeout=4)

    def test_slow_unix_headers_have_bounded_workers_and_absolute_deadline(self):
        """SYNTHETIC: prevent a private-socket Slowloris from exhausting TasksMax.

        The old ThreadingMixIn created one daemon thread per partial header;
        48 pending peers reproduced uncontrolled thread growth and teardown
        exceptions. Close all peers before exiting to avoid interpreter races.
        """
        engine, _ = boot()
        gateway = server_for_live(engine)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v36_monitor.sock"
            server = server_for_operator(engine, gateway, path)
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            peers = []
            try:
                for _ in range(48):
                    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    peer.settimeout(0.4)
                    try:
                        peer.connect(str(path))
                        peer.sendall(
                            b"GET /api/v36/health HTTP/1.1\r\nHost: incomplete"
                        )
                        peers.append(peer)
                    except (TimeoutError, OSError):
                        peer.close()
                self.assertGreaterEqual(len(peers), 16)
                time.sleep(0.12)
                worker_count = sum(
                    "process_request_thread" in t.name for t in active_threads()
                )
                self.assertLessEqual(worker_count, MAX_OPERATOR_WORKERS)
                # All accepted slow peers must be closed by an absolute
                # request deadline, without waiting for the rest of a header.
                time.sleep(OPERATOR_HEADER_DEADLINE_SECONDS + 0.25)
                code, health = private_request(path, "/api/v36/health")
                self.assertEqual(code, 503)
                self.assertFalse(health["operationally_healthy"])
                self.assertLessEqual(
                    sum("process_request_thread" in t.name for t in active_threads()), 2
                )
            finally:
                for peer in peers:
                    peer.close()
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)
        gateway.server_close()

    def test_slow_drip_header_cannot_renew_its_deadline(self):
        engine, _ = boot()
        gateway = server_for_live(engine)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v36_monitor.sock"
            server = server_for_operator(engine, gateway, path)
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            peer.settimeout(2)
            try:
                peer.connect(str(path))
                peer.sendall(b"GET /api/v36/health HTTP/1.1\r\nHost:")
                start = time.monotonic()
                closed = False
                while time.monotonic() - start < 2:
                    time.sleep(0.13)
                    try:
                        peer.sendall(b"a")
                        if peer.recv(1) == b"":
                            closed = True
                            break
                    except (BrokenPipeError, ConnectionResetError):
                        closed = True
                        break
                    except TimeoutError:
                        pass
                self.assertTrue(closed, "Slow-drip request was not closed")
                self.assertLess(time.monotonic() - start, 1.7)
                code, h = private_request(path, "/api/v36/health")
                self.assertEqual(code, 503)
                self.assertFalse(h["operationally_healthy"])
            finally:
                peer.close()
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)
        gateway.server_close()

    def test_existing_path_is_rejected_without_overwrite(self):
        engine, _ = boot()
        server = server_for_live(engine)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v36_monitor.sock"
            path.write_text("do-not-overwrite")
            with self.assertRaises(FileExistsError):
                server_for_operator(engine, server, path)
            self.assertEqual(path.read_text(), "do-not-overwrite")
            path.unlink()
            path.symlink_to("/dev/null")
            with self.assertRaises(FileExistsError):
                server_for_operator(engine, server, path)
        server.server_close()


if __name__ == "__main__":
    unittest.main()
