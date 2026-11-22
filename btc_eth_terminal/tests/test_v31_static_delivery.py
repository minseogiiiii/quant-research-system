"""Verified production bundle delivery and replay UI fail-closed contract.

The browser DOM/network tests are separate and must be executed explicitly.
This suite tests the actual local HTTP handler with the pinned CI-generated
React/Funnest dist while keeping historical market data and trading read-only.
"""

from __future__ import annotations

import re
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import bridge
import v30_unified_api as api


class V31StaticHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)

    def get(self, path):
        try:
            with urlopen(self.base + path, timeout=7) as response:
                return response.status, response.read(), response.headers
        except HTTPError as e:
            return e.code, e.read(), e.headers

    def test_react_entry_is_served_readonly(self):
        status, data, headers = self.get("/research")
        self.assertEqual(status, 200)
        from v33_frontend_integrity import DIST as LEGACY_PINNED_DIST

        self.assertEqual(data, (LEGACY_PINNED_DIST / "index.html").read_bytes())
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_index_alias_is_identical(self):
        self.assertEqual(self.get("/research")[1], self.get("/research/index.html")[1])

    def test_funnest_build_assets_are_real_and_only_two(self):
        _, index, _ = self.get("/research")
        names = [
            name.decode()
            for name in re.findall(rb"/assets/(index-[\w-]+\.(?:css|js))", index)
        ]
        self.assertEqual(len(names), 2)
        self.assertEqual(set(name.rsplit(".", 1)[-1] for name in names), {"css", "js"})
        for name in names:
            status, data, headers = self.get("/assets/" + name)
            self.assertEqual(status, 200)
            from v33_frontend_integrity import DIST as LEGACY_PINNED_DIST

            self.assertEqual(data, (LEGACY_PINNED_DIST / "assets" / name).read_bytes())
            self.assertGreater(len(data), 100)
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

    def test_unknown_static_files_are_denied(self):
        for path in [
            "/assets/config.json",
            "/assets/secret.js",
            "/assets/../../backend/manifest.json",
            "/backend/manifest.json",
            "/frontend/src/unified.tsx",
            "/research/../backend/manifest.json",
        ]:
            with self.subTest(path=path):
                self.assertNotEqual(self.get(path)[0], 200)

    def test_query_args_on_build_files_are_denied(self):
        index = self.get("/research")[1]
        name = re.search(rb"/assets/(index-[\w-]+\.js)", index).group(1).decode()
        self.assertEqual(self.get("/research?admin=true")[0], 400)
        self.assertEqual(self.get("/assets/" + name + "?download=1")[0], 400)

    def test_policy_blocks_external_scripts_and_frames(self):
        _, _, h = self.get("/research")
        csp = h.get("Content-Security-Policy", "")
        for rule in [
            "script-src 'self'",
            "connect-src 'self'",
            "object-src 'none'",
            "frame-ancestors 'none'",
        ]:
            self.assertIn(rule, csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertEqual(h.get("X-Frame-Options"), "DENY")

    def test_legacy_terminal_remains_independent(self):
        status, html, h = self.get("/terminal")
        self.assertEqual(status, 200)
        self.assertIn(b"Observed trade-price candles", html)
        self.assertNotIn(b"@funnest", html)

    def test_source_content_cannot_be_fetched_by_static_route(self):
        self.assertNotEqual(self.get("/assets/index-../../manifest.json")[0], 200)
        self.assertNotEqual(
            self.get("/backend/data/unified_v30_one_hour/projection.json")[0], 200
        )


class V31ReplayUIContracts(unittest.TestCase):
    def test_replay_zero_completed_bars_has_explicit_message(self):
        src = (ROOT / "frontend/src/unified.tsx").read_text()
        self.assertIn("visibleCandleCount>0", src)
        self.assertIn("No completed 5-minute receiver-time candles", src)
        self.assertIn("loadedProduct===product", src)

    def test_pending_cutoff_hides_old_observations(self):
        src = (ROOT / "frontend/standalone/app.js").read_text()
        self.assertIn(
            "$('verified-views').hidden=true;words($('replay-state'),'VERIFYING REPLAY…')",
            src,
        )
        self.assertIn("$('verified-views').hidden=false;redrawMetrics()", src)
        self.assertIn("epoch!==state.requestEpoch", src)

    def test_replay_cutoff_counts_match_server_causality(self):
        c = api.catalog()
        start, stop = c["source_start_ms"], c["source_end_ms"] + 1
        for product in ("BTC-USD", "ETH-USD"):
            data = api.series(product, "candles", start, stop)["rows"]
            grid = api.series(product, "features", start, stop)["rows"]
            self.assertEqual(len(data), 11)
            for feature in grid:
                cutoff = feature["decision_receive_utc_ms"]
                rows = api.series(product, "candles", start, stop, asof_ms=cutoff)[
                    "rows"
                ]
                self.assertEqual(
                    len(rows), sum(x["available_at"] <= cutoff for x in data)
                )


if __name__ == "__main__":
    unittest.main()
