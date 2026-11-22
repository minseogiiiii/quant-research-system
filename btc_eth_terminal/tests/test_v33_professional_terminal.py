"""v33 real-archive workspace and fail-closed pin regression tests.

No synthetic alpha, simulated trade execution, or new market-source data.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
def _q14_load_after_path_setup_0():
    import bridge
    import v30_unified_api as api
    import v32_frontend_integrity as legacy_pin
    import v33_frontend_integrity as pin
    return bridge, api, legacy_pin, pin

bridge, api, legacy_pin, pin = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0


class VerifiedProfessionalBuild(unittest.TestCase):
    def test_real_released_bundle_three_files(self):
        files, m = pin.verify_build()
        self.assertEqual(len(files), 3)
        self.assertFalse(m["live_trading_enabled"])
        self.assertTrue(m["receiver_time_only"])
        self.assertEqual(m["git_commit"], "6e88f9c9811ff42406a3259f7ac82fc1e8719f03")
        self.assertIn("index.html", files)
        self.assertEqual(len([p for p in files if p.endswith(".js")]), 1)
        self.assertEqual(len([p for p in files if p.endswith(".css")]), 1)

    def test_preserved_prior_v32_bundle_unchanged(self):
        _, old = legacy_pin.verify_build()
        self.assertEqual(old["schema"], "v32_funnest_build_integrity_v1")

    def test_source_scope_btc_eth_only(self):
        c = api.catalog()
        self.assertEqual({x["product"] for x in c["products"]}, {"BTC-USD", "ETH-USD"})
        self.assertFalse(c["live_feed"])
        self.assertFalse(c["trading_enabled"])
        self.assertFalse(c["oos_approved"])
        self.assertFalse(c["original_feature_label_pit_verified"])

    def test_real_trade_and_causal_diag_same_receiver_cutoff(self):
        c = api.catalog()
        start, stop = c["source_start_ms"], c["source_end_ms"] + 1
        for product in ("BTC-USD", "ETH-USD"):
            features = api.series(product, "features", start, stop)["rows"]
            candles = api.series(product, "candles", start, stop)["rows"]
            self.assertEqual(len(features), 12)
            self.assertEqual(len(candles), 11)
            for f in features:
                ts = f["decision_receive_utc_ms"]
                self.assertFalse(f["eligible_for_oos_or_trading"])
                rows = api.series(product, "candles", start, stop, asof_ms=ts)["rows"]
                tape = api.series(product, "trades", start, stop, asof_ms=ts)["rows"]
                self.assertEqual(
                    len(rows), sum(x["available_at"] <= ts for x in candles)
                )
                self.assertTrue(all(x["received_at_ms"] <= ts for x in tape))
                self.assertTrue(all(x["available_at"] <= ts for x in rows))
                self.assertTrue(
                    all(x["time"] + 300000 <= x["available_at"] for x in rows)
                )

    def test_same_source_book_l1_only(self):
        c = api.catalog()
        start, stop = c["source_start_ms"], c["source_end_ms"] + 1
        for product in ("BTC-USD", "ETH-USD"):
            f = api.series(product, "features", start, stop)["rows"][-1]
            b = api.book(product, f["decision_receive_utc_ms"])
            self.assertTrue(b["l1_only"])
            self.assertEqual(b["book"]["best_bid"], f["best_bid"])
            self.assertEqual(b["book"]["microprice"], f["microprice"])
            self.assertFalse(b["live_feed"])
            self.assertFalse(b["trading_enabled"])

    def test_tsx_renders_provenance_bound_components(self):
        # Legacy v33 textual UI claims must be checked against the actually served,
        # SHA-pinned v33 bundle, not the evolving v36 TypeScript worktree.
        served, _ = pin.verify_build()
        js = next(
            data.decode("utf-8")
            for name, data in served.items()
            if name.endswith(".js")
        )
        for phrase in (
            "HISTORICAL MARKET REPLAY",
            "No completed 5-minute receiver-time candles",
            "Order book · L1",
            "OFI · five-minute rolling",
            "Microprice deviation",
            "Liquidity recovery",
            "Recorded trade tape",
            "Data quality & admission",
            "Research results",
            "Evidence fingerprint",
            "NO CONFIRMED EXECUTABLE ALPHA",
            "NO ADDITIONAL L2 LEVELS IN PROJECTION",
        ):
            self.assertIn(phrase, js)
        # Retain independent causal availability checks on the current source.
        src = (ROOT / "frontend/src/unified.tsx").read_text()
        self.assertIn("const asof=cutoff;", src)
        self.assertIn("loadedProduct===product?features:[]", src)
        self.assertIn("c.available_at<=asof", src)
        self.assertIn("visibleCandleCount>0", src)

    def test_clocks_and_safe_replay_have_real_controls(self):
        s = (ROOT / "frontend/src/unified.tsx").read_text()
        for phrase in (
            "Play historical replay",
            "Pause historical replay",
            "Step historical replay",
            "Reset historical replay",
            "window.clearInterval(timer)",
            "t.received_at_ms<=asof",
            "f.decision_receive_utc_ms<=asof",
            "getBars",
        ):
            self.assertIn(phrase, s)

    def test_legacy_history_remains_isolated(self):
        s = (ROOT / "frontend/src/main.tsx").read_text()
        self.assertIn("legacyOpen,setLegacyOpen", s)
        self.assertIn("legacy-source-warning", s)
        self.assertIn("Separate historical CSV sandbox", s)

    def test_integrity_rejects_forged_js(self):
        with tempfile.TemporaryDirectory() as temp:
            dist = Path(temp) / "dist"
            shutil.copytree(pin.DIST, dist)
            js = next((dist / "assets").glob("*.js"))
            js.write_bytes(js.read_bytes() + b"\n// injected")
            with self.assertRaises(pin.FrontendIntegrityError):
                pin.get_asset("/research", dist=dist)

    def test_no_extra_browser_asset_route(self):
        with self.assertRaises(pin.FrontendIntegrityError):
            pin.get_asset("/assets/admin.js")

    def test_css_layout_is_responsive(self):
        s = (ROOT / "frontend/src/style.css").read_text()
        self.assertIn(".pro-main-grid", s)
        self.assertIn("@media(max-width:780px)", s)
        self.assertIn(".pro-bottom-grid", s)


class VerifiedHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def get(self, path):
        try:
            with urlopen(self.url + path, timeout=8) as r:
                return r.status, r.read(), r.headers
        except HTTPError as e:
            return e.code, e.read(), e.headers

    def test_v33_research_serves_exact_pinned_html(self):
        status, data, h = self.get("/research")
        self.assertEqual(status, 200)
        self.assertEqual(data, pin.verify_build()[0]["index.html"])
        self.assertIn("connect-src 'self'", h.get("Content-Security-Policy", ""))

    def test_v33_assets_pinned_and_no_bypasses(self):
        for name, data in pin.verify_build()[0].items():
            if name == "index.html":
                continue
            self.assertEqual(self.get("/" + name)[:2], (200, data))
        for bad in (
            "/assets/old.js",
            "/frontend/src/unified.tsx",
            "/frontend/v33_dist_integrity.json",
            "/research?load=admin",
        ):
            self.assertNotEqual(self.get(bad)[0], 200)

    def test_single_asset_mutation_blocks_all_http_serving(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            shutil.copytree(pin.DIST, dist)
            js = next((dist / "assets").glob("*.js"))
            js.write_bytes(js.read_bytes() + b"INJECTED")
            orig = pin.get_asset

            def bad(path):
                return orig(path, dist=dist)

            with patch("v33_frontend_integrity.get_asset", side_effect=bad):
                self.assertEqual(self.get("/research")[0], 503)
                self.assertEqual(
                    self.get("/" + js.relative_to(dist).as_posix())[0], 503
                )


if __name__ == "__main__":
    unittest.main()
