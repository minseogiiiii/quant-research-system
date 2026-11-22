"""Regression: eight persistent SSE streams cannot block health and metrics."""

import json
import sys
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from v36_live_gateway import server_for_live

sys.path.insert(0, str(ROOT / "tests"))
from test_v36_ci_synthetic_admission import boot


class LiveHealthReserve(unittest.TestCase):
    def test_eight_sse_subscriptions_leave_two_http_monitoring_slots(self):
        engine, _ = boot()
        server = server_for_live(engine)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        host = f"http://127.0.0.1:{server.server_address[1]}"
        clients = []
        try:
            for _ in range(8):
                response = urlopen(host + "/api/v36/stream?product=BTC-USD", timeout=3)
                self.assertEqual(response.status, 200)
                clients.append(response)
            deadline = time.monotonic() + 2
            while (
                time.monotonic() < deadline
                and server.metrics()["active_sse_connections"] < 8
            ):
                time.sleep(0.02)
            self.assertEqual(server.metrics()["active_sse_connections"], 8)
            with self.assertRaises(HTTPError) as cm:
                urlopen(host + "/api/v36/stream?product=BTC-USD", timeout=3)
            self.assertEqual(cm.exception.code, 503)
            # R4 rejected health under eight SSE streams with RemoteDisconnected.
            for _ in range(20):
                with urlopen(host + "/api/v36/health", timeout=2) as r:
                    self.assertEqual(r.status, 200)
                    self.assertEqual(json.load(r)["status"], "HEALTHY")
                with urlopen(host + "/api/v36/metrics", timeout=2) as r:
                    m = json.load(r)
                    self.assertEqual(m["max_clients"], 10)
                    self.assertEqual(m["max_sse_clients"], 8)
                    self.assertEqual(m["active_sse_connections"], 8)
                    self.assertGreaterEqual(m["sse_rejected_slots"], 1)
                    self.assertFalse(m["trading_enabled"])
            self.assertLessEqual(server.metrics()["peak_connections"], 10)
        finally:
            for c in clients:
                c.close()
            engine.transport_lost("SYNTHETIC_STOP")
            deadline = time.monotonic() + 5
            while (
                time.monotonic() < deadline
                and server.metrics()["active_sse_connections"]
            ):
                time.sleep(0.05)
            self.assertEqual(server.metrics()["active_sse_connections"], 0)
            server.shutdown()
            server.server_close()
            worker.join(timeout=4)


if __name__ == "__main__":
    unittest.main()
