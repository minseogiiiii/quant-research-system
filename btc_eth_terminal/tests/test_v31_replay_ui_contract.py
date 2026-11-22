"""Replay event-time and cross-product contracts backing v31 browser QA.

Production Funnest/Chromium interactions are tested separately by
scripts/v31_browser_contract.py with real-source API responses.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def _q14_load_after_path_setup_0():
    import v30_unified_api as api

    return api


api = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0


class V31ReplayContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = api.catalog()
        cls.start = cls.catalog["source_start_ms"]
        cls.stop = cls.catalog["source_end_ms"] + 1

    def test_readonly_archive(self):
        self.assertFalse(self.catalog["live_feed"])
        self.assertFalse(self.catalog["trading_enabled"])
        self.assertFalse(self.catalog["oos_approved"])
        self.assertFalse(
            self.catalog["point_in_time_historical_available_at_certified"]
        )

    def test_instrument_scope(self):
        self.assertEqual(
            {p["product"] for p in self.catalog["products"]}, {"BTC-USD", "ETH-USD"}
        )

    def test_candle_asof_boundary_by_product(self):
        for product in ("BTC-USD", "ETH-USD"):
            all_bars = api.series(product, "candles", self.start, self.stop)["rows"]
            self.assertEqual(len(all_bars), 11)
            for bar in all_bars:
                with self.subTest(product=product, available_at=bar["available_at"]):
                    before = api.series(
                        product,
                        "candles",
                        self.start,
                        self.stop,
                        asof_ms=bar["available_at"] - 1,
                    )["rows"]
                    at = api.series(
                        product,
                        "candles",
                        self.start,
                        self.stop,
                        asof_ms=bar["available_at"],
                    )["rows"]
                    self.assertNotIn(bar, before)
                    self.assertIn(bar, at)
                    self.assertEqual(bar["available_at"], bar["time"] + 300000)

    def test_first_feature_cannot_see_candle_before_full_close(self):
        for product in ("BTC-USD", "ETH-USD"):
            feature = api.series(product, "features", self.start, self.stop)["rows"][0]
            bars = api.series(
                product,
                "candles",
                self.start,
                self.stop,
                asof_ms=feature["decision_receive_utc_ms"],
            )["rows"]
            self.assertEqual(bars, [])

    def test_cutoff_trade_sample_causality(self):
        for product in ("BTC-USD", "ETH-USD"):
            features = api.series(product, "features", self.start, self.stop)["rows"]
            for entry in features:
                cutoff = entry["decision_receive_utc_ms"]
                trades = api.series(
                    product, "trades", self.start, self.stop, asof_ms=cutoff
                )["rows"]
                self.assertTrue(all(t["received_at_ms"] <= cutoff for t in trades))
                self.assertTrue(all(t["product"] == product for t in trades))

    def test_candle_count_monotonic_over_replay_grid(self):
        for product in ("BTC-USD", "ETH-USD"):
            times = [
                f["decision_receive_utc_ms"]
                for f in api.series(product, "features", self.start, self.stop)["rows"]
            ]
            counts = [
                len(
                    api.series(product, "candles", self.start, self.stop, asof_ms=t)[
                        "rows"
                    ]
                )
                for t in times
            ]
            self.assertEqual(counts, sorted(counts))
            self.assertEqual(counts[0], 0)
            self.assertEqual(counts[-1], 11)

    def test_invalid_source_product_never_falls_back(self):
        for instrument in ("BTCUSDT", "ETHUSDT", "BTC-PERP", "", None):
            with (
                self.subTest(instrument=instrument),
                self.assertRaises(api.UnifiedAPIError),
            ):
                api.series(instrument, "candles", self.start, self.stop)

    def test_missing_quote_not_forward_filled(self):
        for product in ("BTC-USD", "ETH-USD"):
            feat = api.series(product, "features", self.start, self.stop)["rows"][3]
            cutoff = feat["decision_receive_utc_ms"]
            self.assertIsNotNone(api.book(product, cutoff)["book"])
            self.assertIsNone(api.book(product, cutoff + 5001)["book"])

    def test_ui_standalone_latest_response_only(self):
        js = (ROOT / "frontend/standalone/app.js").read_text()
        for phrase in (
            "++state.requestEpoch",
            "epoch!==state.requestEpoch",
            "result.product!==product",
            "if(epoch===state.requestEpoch)danger(e)",
            "Mixed instrument rows rejected",
        ):
            self.assertIn(phrase, js)

    def test_ui_react_archive_separation(self):
        app = (ROOT / "frontend/src/main.tsx").read_text()
        view = (ROOT / "frontend/src/unified.tsx").read_text()
        self.assertIn("legacyOpen,setLegacyOpen", app)
        self.assertIn('{legacyOpen ? <section className="legacy-sandbox">', app)
        self.assertIn("setLoadedProduct(null)", view)
        self.assertIn("loadedProduct===product?features:[]", view)
        self.assertIn("c.available_at<=asof", view)

    def test_fixed_readonly_projection_sha(self):
        p, sha = api.load_projection()
        self.assertEqual(sha, self.catalog["projection_sha256"])
        self.assertFalse(p["source_clock_independently_calibrated"])
        self.assertFalse(p["execution_authorized"])

    def test_no_pit_or_oos_promotion(self):
        for product in ("BTC-USD", "ETH-USD"):
            rows = api.series(product, "features", self.start, self.stop)["rows"]
            self.assertTrue(
                all(r["eligible_for_oos_or_trading"] is False for r in rows)
            )


if __name__ == "__main__":
    unittest.main()
