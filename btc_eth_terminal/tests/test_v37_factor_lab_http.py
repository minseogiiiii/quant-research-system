"""Q7 actual loopback HTTP route, historical asset preservation, and fail closed."""

import json
import sys
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from v34_frontend_integrity import verify_build
from v36_frontend_integrity import LIVE_JS, v36_asset
from v36_live_engine import LiveMarketEngine
from v36_live_gateway import server_for_live
from v37_factor_lab import load_report
from v37_frontend_integrity import V37_CSS, V37_JS, v37_asset


class Q7EndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.e = LiveMarketEngine()
        cls.server = server_for_live(cls.e)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=3)

    def get(self, path):
        try:
            with urlopen(self.base + path, timeout=5) as r:
                return r.status, r.read(), dict(r.headers)
        except HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def test_q7_assets_exact_and_historical_assets_unmodified(self):
        for path in ("/research/v37", V37_JS, V37_CSS):
            status, data, headers = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertEqual(data, v37_asset(path)[0], path)
            self.assertIn("nosniff", headers["X-Content-Type-Options"])
        self.assertEqual(self.get("/research/v36")[1], v36_asset("/research/v36")[0])
        self.assertEqual(self.get(LIVE_JS)[1], v36_asset(LIVE_JS)[0])
        self.assertIsNotNone(verify_build())

    def test_factor_research_endpoint_identity_and_no_fake_pnl(self):
        obj, sha = load_report()
        t = obj["source_start_receiver_ms"] + 120000
        for p in ("BTC-USD", "ETH-USD"):
            path = f"/api/v37/factor-lab?product={p}&asof={t}&section=research"
            code, raw, _ = self.get(path)
            self.assertEqual(code, 200, raw[:100])
            resp = json.loads(raw)
            self.assertEqual(resp["source_sha256"], obj["source_manifest_sha256"])
            self.assertEqual(resp["report_sha256"], sha)
            self.assertEqual(resp["verdict"], "INSUFFICIENT_EVIDENCE")
            self.assertIsNone(resp["cost_adjusted_pnl"])
            self.assertLess(resp["summary"]["feature_rows"], 60)
            self.assertFalse(resp["real_orders_enabled"])

    def test_v37_uses_existing_research_app_with_factor_lab(self):
        frontend = Path(__file__).resolve().parents[1] / "frontend"
        main = (frontend / "src/main.tsx").read_text()
        unified = (frontend / "src/unified.tsx").read_text()
        bundle = v37_asset(V37_JS)[0].decode("utf-8")
        # This regression caught a real earlier error: v37 was sent to LiveTerminal,
        # so the FactorLabPanel was compiled but never mounted on the v37 route.
        self.assertIn(
            "window.location.pathname==='/research/v36') ? <LiveTerminal/> : <App/>",
            main,
        )
        self.assertNotIn("window.location.pathname==='/research/v37'", main)
        self.assertIn("<UnifiedTerminal />", main)
        self.assertIn("<FactorLabPanel product={product}", unified)
        self.assertIn("OFI & liquidity recovery research", bundle)
        self.assertIn("/api/v37/factor-lab", bundle)
        factor = (frontend / "src/FactorLabPanel.tsx").read_text()
        self.assertIn("if(controller.signal.aborted)return;", factor)
        self.assertIn("result.asof_receiver_ms===asof", factor)
        self.assertIn("result.source_sha256===sourceSha", factor)
        self.assertIn("{visible&&<>", factor)

    def test_future_not_visible_and_no_order_routes(self):
        obj, _ = load_report()
        t = obj["source_start_receiver_ms"] + 125000
        _, raw, _ = self.get(
            f"/api/v37/factor-lab?product=BTC-USD&asof={t}&section=microstructure"
        )
        r = json.loads(raw)
        self.assertTrue(all(row["decision_receiver_ms"] <= t for row in r["rows"]))
        self.assertTrue(
            all("future_only_diagnostic_labels" not in row for row in r["rows"])
        )
        for path in (
            "/api/v37/order",
            "/api/v37/factor-lab?product=SOL-USD&asof=1794642300000&section=research",
            "/api/v37/factor-lab?product=BTC-USD&asof=1794642300000&section=unknown",
            "/api/v37/factor-lab?product=BTC-USD&product=ETH-USD&asof=1794642300000&section=research",
        ):
            code, body, _ = self.get(path)
            self.assertNotEqual(code, 200, path)

    def test_query_asset_rejected(self):
        self.assertEqual(self.get("/research/v37?disable_integrity=1")[0], 503)


if __name__ == "__main__":
    unittest.main()
