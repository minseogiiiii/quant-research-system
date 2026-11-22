"""Safety, provenance, deterministic cutoff, and HTTP boundary tests.
Real-source double replay is exercised by the v30 projector, not mocked here.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
def _q14_load_after_path_setup_0():
    import bridge
    import v30_unified_api as api
    from v30_unified_projection import PRODS, SCHEMA
    return bridge, api, PRODS, SCHEMA

bridge, api, PRODS, SCHEMA = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0


class UnifiedContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = api.catalog()

    def test_products_strict(self):
        self.assertEqual([x["product"] for x in self.c["products"]], list(PRODS))

    def test_historical_only(self):
        self.assertFalse(self.c["live_feed"])
        self.assertFalse(self.c["trading_enabled"])
        self.assertFalse(self.c["oos_approved"])

    def test_provenance_bound(self):
        self.assertEqual(len(self.c["source_sha256"]), 64)
        self.assertEqual(len(self.c["projection_sha256"]), 64)
        self.assertEqual(self.c["source_message_count"], 152069)

    def test_projection_digest(self):
        raw = api.PROJECTION.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), self.c["projection_sha256"])
        self.assertEqual(json.loads(raw)["schema"], SCHEMA)

    def test_no_unverified_pit_promotion(self):
        self.assertFalse(self.c["point_in_time_historical_available_at_certified"])
        self.assertFalse(self.c["original_feature_label_pit_verified"])

    def test_btc_eth_real_source_rows(self):
        for prod in PRODS:
            with self.subTest(prod=prod):
                p = api.series(prod, "candles", 0, 8_000_000_000_000)
                self.assertEqual(len(p["rows"]), 11)
                self.assertFalse(p["oos_approved"])
                self.assertTrue(
                    all(
                        float(x["low"]) <= float(x["close"]) <= float(x["high"])
                        for x in p["rows"]
                    )
                )

    def test_features_only_admissible_receive_grid(self):
        for prod in PRODS:
            rows = api.series(prod, "features", 0, 8_000_000_000_000)["rows"]
            self.assertEqual(len(rows), 12)
            self.assertTrue(
                all(
                    row["feature_max_received_utc_ms"] <= row["decision_receive_utc_ms"]
                    for row in rows
                    if row["feature_max_received_utc_ms"] is not None
                )
            )
            self.assertTrue(
                all(row["eligible_for_oos_or_trading"] is False for row in rows)
            )

    def test_cutoff_prevents_incomplete_candle(self):
        prod = "BTC-USD"
        rows = api.series(prod, "candles", 0, 8_000_000_000_000)["rows"]
        first = rows[0]
        self.assertEqual(
            api.series(
                prod, "candles", 0, 8_000_000_000_000, asof_ms=first["available_at"] - 1
            )["rows"],
            [],
        )
        self.assertEqual(
            api.series(
                prod, "candles", 0, 8_000_000_000_000, asof_ms=first["available_at"]
            )["rows"][0],
            first,
        )

    def test_book_cutoff_with_stale_quote_is_withheld(self):
        f = api.series("BTC-USD", "features", 0, 8_000_000_000_000)["rows"][3]
        t = f["decision_receive_utc_ms"]
        self.assertIsNotNone(api.book("BTC-USD", t)["book"])
        self.assertIsNone(api.book("BTC-USD", t + 5001)["book"])

    def test_no_future_feature(self):
        first = api.series("BTC-USD", "features", 0, 8_000_000_000_000)["rows"][0]
        self.assertEqual(
            api.series(
                "BTC-USD",
                "features",
                0,
                8_000_000_000_000,
                asof_ms=first["decision_receive_utc_ms"] - 1,
            )["rows"],
            [],
        )

    def test_trade_update_counts_crosschecked_with_independent_replay(self):
        self.assertEqual(
            {p["product"]: p["observed_trade_updates"] for p in self.c["products"]},
            {"BTC-USD": 51872, "ETH-USD": 8835},
        )

    def test_bounded_trade_samples_span_historical_grid(self):
        for prod in PRODS:
            rows = api.series(prod, "trades", 0, 8_000_000_000_000, limit=400)["rows"]
            self.assertTrue(100 <= len(rows) <= 400)
            self.assertGreaterEqual(
                len({x["received_at_ms"] // 300000 for x in rows}), 10
            )
            self.assertEqual(len({x["trade_id"] for x in rows}), len(rows))

    def test_offset_does_not_duplicate_same_millisecond_trade(self):
        rows = api.series("BTC-USD", "trades", 0, 8_000_000_000_000, limit=400)["rows"]
        first = api.series(
            "BTC-USD", "trades", 0, 8_000_000_000_000, limit=17, offset=0
        )["rows"]
        second = api.series(
            "BTC-USD", "trades", 0, 8_000_000_000_000, limit=17, offset=17
        )["rows"]
        self.assertEqual(first + second, rows[:34])

    def test_page_offset_deterministic(self):
        whole = api.series("ETH-USD", "trades", 0, 8_000_000_000_000, limit=400)["rows"]
        a = api.series("ETH-USD", "trades", 0, 8_000_000_000_000, limit=5)
        b = api.series("ETH-USD", "trades", 0, 8_000_000_000_000, limit=5, offset=5)
        self.assertEqual(a["next_offset"], 5)
        self.assertEqual(a["rows"] + b["rows"], whole[:10])
        self.assertTrue(all(x["product"] == "ETH-USD" for x in whole))

    def test_series_invalid(self):
        bad = [
            ("BTCUSDT", "candles", 0, 100, 5),
            ("BTC-USD", "fills", 0, 100, 5),
            ("BTC-USD", "candles", 101, 100, 5),
            ("BTC-USD", "candles", 0, 100, 401),
            ("BTC-USD", "candles", 0, 100, 0),
        ]
        for obj in bad:
            with self.subTest(obj=obj), self.assertRaises(api.UnifiedAPIError):
                api.series(*obj)

    def test_offset_invalid(self):
        for off in [-1, 50001, 1.5, True]:
            with self.subTest(off=off), self.assertRaises(api.UnifiedAPIError):
                api.series("BTC-USD", "trades", 0, 100, 10, offset=off)

    def test_projection_mutation_fails_closed(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "projection.json"
            p.write_bytes(api.PROJECTION.read_bytes() + b" ")
            with (
                patch.object(api, "PROJECTION", p),
                self.assertRaises(api.UnifiedAPIError),
            ):
                api.load_projection()

    def test_source_manifest_mutation_fails_closed(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "manifest.json"
            p.write_bytes(b"{}")
            with (
                patch.object(api, "BASE_ROOT", Path(tmp)),
                self.assertRaises(api.UnifiedAPIError),
            ):
                api.load_projection()

    def test_pin_mutation_fails_closed(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "sha.json"
            p.write_text("{}")
            with patch.object(api, "PIN", p), self.assertRaises(api.UnifiedAPIError):
                api.load_projection()

    def test_symlink_projection_fails_closed(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "p.json"
            p.symlink_to(api.PROJECTION)
            with (
                patch.object(api, "PROJECTION", p),
                self.assertRaises(api.UnifiedAPIError),
            ):
                api.load_projection()

    def test_book_only_two_symbols(self):
        with self.assertRaises(api.UnifiedAPIError):
            api.book("BTCUSDT", 1794645000000)


class BridgeContract(unittest.TestCase):
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
            with urlopen(self.base + path, timeout=10) as r:
                return r.status, r.read(), r.headers
        except HTTPError as e:
            return e.code, e.read(), e.headers

    def test_catalog_200_real_source(self):
        code, raw, _ = self.get("/api/v30/catalog")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["source_message_count"], 152069)

    def test_bars_200(self):
        code, raw, _ = self.get(
            "/api/v30/series?product=BTC-USD&kind=candles&from=1794640000000&to=1794650000000"
        )
        self.assertEqual(code, 200)
        self.assertEqual(len(json.loads(raw)["rows"]), 11)

    def test_book_200(self):
        code, raw, _ = self.get("/api/v30/book?product=BTC-USD&asof=1794642600000")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["book"]["instrument"], "BTC-USD")

    def test_duplicate_query_blocked(self):
        self.assertEqual(
            self.get(
                "/api/v30/series?product=BTC-USD&product=ETH-USD&kind=candles&from=0&to=1800000000000"
            )[0],
            400,
        )

    def test_unknown_query_blocked(self):
        self.assertEqual(self.get("/api/v30/catalog?admin=true")[0], 400)

    def test_directory_not_exposed(self):
        self.assertEqual(
            self.get("/backend/data/live_v16_one_hour/manifest.json")[0], 404
        )

    def test_unsupported_product_blocked(self):
        self.assertEqual(
            self.get("/api/v30/book?product=BTCUSDT&asof=1794642300000")[0], 400
        )

    def test_static_terminal_served(self):
        code, raw, headers = self.get("/terminal")
        self.assertEqual(code, 200)
        self.assertIn(b"Archived", raw)
        self.assertIn("default-src", headers.get("Content-Security-Policy"))
        for path in ["/terminal/styles.css", "/terminal/app.js"]:
            self.assertEqual(self.get(path)[0], 200)

    def test_terminal_path_attack_blocked(self):
        self.assertEqual(self.get("/terminal/../../backend/manifest.json")[0], 404)

    def test_css_js_no_remote_dependencies(self):
        js = (ROOT / "frontend/standalone/app.js").read_text()
        self.assertNotIn("https://", js)
        self.assertIn("/api/v30/catalog", js)


if __name__ == "__main__":
    unittest.main()
