"""Synthetic transport-only tests: no market observations are fabricated."""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler
import socket
from threading import Thread
import time
import unittest

from v35_unified_gateway import BoundedServer, CLIENT_SOCKET_TIMEOUT_SECONDS


class Reply(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'OK')

    def log_message(self, *args):
        pass


class BoundedTransport(unittest.TestCase):
    def setUp(self):
        self.server = BoundedServer(('127.0.0.1', 0), Reply)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=6)
        self.assertFalse(self.thread.is_alive())

    def test_default_budgets_and_safe_identity(self):
        m = self.server.metrics()
        self.assertEqual(m['schema'], 'v35_1_finite_gateway_metrics_v1')
        self.assertEqual(m['max_clients'], 8)
        self.assertEqual(m['socket_timeout_seconds'], CLIENT_SOCKET_TIMEOUT_SECONDS)
        self.assertEqual(m['sse_queue_depth'], 0)
        self.assertFalse(m['live_feed'])
        self.assertFalse(m['trading_enabled'])
        self.assertEqual(m['active_connections'], 0)

    def test_complete_http_request_updates_counters(self):
        with socket.create_connection(self.server.server_address, timeout=3) as sock:
            sock.sendall(b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')
            self.assertIn(b'200 OK', sock.recv(2048))
        deadline = time.monotonic() + 2
        while self.server.metrics()['active_connections'] and time.monotonic() < deadline:
            time.sleep(.02)
        m = self.server.metrics()
        self.assertGreaterEqual(m['accepted_requests'], 1)
        self.assertEqual(m['active_connections'], 0)

    def test_idle_header_does_not_hold_workers_forever(self):
        peers = []
        try:
            for _ in range(2):
                sock = socket.create_connection(self.server.server_address, timeout=3)
                sock.sendall(b'GET / HTTP/1.1\r\nHost: localhost\r\nX-Partial: ')
                peers.append(sock)
            deadline = time.monotonic() + 2
            while self.server.metrics()['active_connections'] < 2 and time.monotonic() < deadline:
                time.sleep(.03)
            self.assertEqual(self.server.metrics()['active_connections'], 2)
            deadline = time.monotonic() + CLIENT_SOCKET_TIMEOUT_SECONDS + 3
            while self.server.metrics()['active_connections'] > 0 and time.monotonic() < deadline:
                time.sleep(.04)
            m = self.server.metrics()
            self.assertEqual(m['active_connections'], 0)
            self.assertLessEqual(m['peak_connections'], m['max_clients'])
        finally:
            for sock in peers:
                sock.close()

    def test_per_server_telemetry_is_independent(self):
        self.server.count('resync_required')
        self.server.count('sse_started')
        other = BoundedServer(('127.0.0.1', 0), Reply)
        try:
            self.assertEqual(other.metrics()['resync_required'], 0)
            self.assertEqual(other.metrics()['sse_started'], 0)
            self.assertEqual(self.server.metrics()['sse_started'], 1)
        finally:
            other.server_close()


if __name__ == '__main__':
    unittest.main()
