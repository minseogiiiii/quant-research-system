"""Q15 fail-closed real HTTP contracts without any private market fixture.

Tests genuine v36 gateway sockets, not mocked HTTP or synthetic price data.
These checks DO NOT certify the shipped browser, live market source or release.
"""

from __future__ import annotations

import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from v36_live_engine import LiveMarketEngine
from v36_live_gateway import server_for_live


class Q15GatewayNoSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = server_for_live(LiveMarketEngine())
        cls.worker = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.worker.start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=5)

    def request(self, route: str, method: str = "GET") -> tuple[int, dict]:
        try:
            with urlopen(
                Request(self.origin + route, method=method), timeout=4
            ) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            if "application/json" in exc.headers.get("Content-Type", ""):
                return exc.code, json.loads(exc.read())
            return exc.code, {"non_json_error": True}

    def test_health_has_no_admitted_source_or_execution(self) -> None:
        status, data = self.request("/api/v36/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "CONNECTING")
        self.assertEqual(data["mode"], "ARCHIVE_SIMULATION")
        self.assertFalse(data["live_feed"])
        self.assertFalse(data["trading_enabled"])
        self.assertFalse(data["execution_authorized"])
        self.assertIsNone(data["session_id"])
        self.assertEqual(data["snapshots"], [])

    def test_btc_eth_quotes_are_503_and_never_fabricated(self) -> None:
        for product in ("BTC-USD", "ETH-USD"):
            with self.subTest(product=product):
                status, data = self.request(
                    f"/api/v36/snapshot?product={product}"
                )
                self.assertEqual(status, 503)
                self.assertIsNone(data["state"])
                self.assertTrue(data["resync_required"])
                self.assertFalse(data["trading_enabled"])
                self.assertFalse(data["execution_authorized"])

    def test_order_routes_never_accept_writes(self) -> None:
        status, data = self.request("/api/v36/orders")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"], "UNKNOWN_READ_ONLY_ROUTE")
        for endpoint in ("/api/v36/order", "/api/v36/submit_order"):
            with self.subTest(endpoint=endpoint):
                status, _ = self.request(endpoint, "POST")
                self.assertEqual(status, 501)

    def test_unknown_product_fails_closed(self) -> None:
        status, data = self.request("/api/v36/snapshot?product=ZZZ")
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "UNKNOWN_PRODUCT")
        self.assertFalse(data["trading_enabled"])

    def test_real_http_metrics_report_bounded_gateway(self) -> None:
        status, data = self.request("/api/v36/metrics")
        self.assertEqual(status, 200)
        self.assertFalse(data["source_healthy"])
        self.assertFalse(data["trading_enabled"])
        self.assertFalse(data["execution_authorized"])
        self.assertLessEqual(data["max_sse_clients"], data["max_clients"])


if __name__ == "__main__":
    unittest.main()
