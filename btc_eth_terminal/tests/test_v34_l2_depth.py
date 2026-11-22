"""Actual archived depth, point-in-time recovery and adversarial tests."""

import json
import sys
import tempfile
import unittest
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from v23_causal_microstructure import Book
from v34_l2_api import L2APIError, depth, load_l2, recovery
from v34_l2_depth import DepthError, shock_events, top_levels


class ReplayTests(unittest.TestCase):
    def test_depth_and_deletion(self):
        b = Book()
        b.apply(
            "snapshot",
            [
                {
                    "side": side,
                    "price_level": str(start + i * step),
                    "new_quantity": str(i + 1),
                }
                for side, start, step in (("bid", 100, -1), ("offer", 101, 1))
                for i in range(20)
            ],
        )
        r = top_levels(b)
        self.assertEqual(len(r["bids"]), 10)
        self.assertEqual(r["asks"][0]["price"], "101")
        self.assertEqual(Decimal(r["bids"][9]["cumulative_base"]), Decimal(55))
        b.apply("update", [{"side": "bid", "price_level": "100", "new_quantity": "0"}])
        self.assertEqual(top_levels(b)["bids"][0]["price"], "99")

    def test_missing_snapshot(self):
        self.assertIsNone(top_levels(Book()))

    def test_one_level_insufficient(self):
        b = Book()
        b.apply(
            "snapshot",
            [
                {"side": "bid", "price_level": "1", "new_quantity": "1"},
                {"side": "offer", "price_level": "2", "new_quantity": "1"},
            ],
        )
        with self.assertRaises(DepthError):
            top_levels(b)

    def test_crossed(self):
        b = Book()
        b.apply(
            "snapshot",
            [
                {
                    "side": side,
                    "price_level": str(start + i * step),
                    "new_quantity": "1",
                }
                for side, start, step in (("bid", 100, -1), ("offer", 99, 1))
                for i in range(10)
            ],
        )
        with self.assertRaises(DepthError):
            top_levels(b)


class ShockTests(unittest.TestCase):
    def samples(self, recovery=True):
        out = [
            {
                "time_ms": 1_800_000_000_000 + i * 1000,
                "depth_base": "100",
                "valid": True,
            }
            for i in range(100)
        ]
        out[40]["depth_base"] = "40"
        if not recovery:
            for i in range(41, 71):
                out[i]["depth_base"] = "60"
        return out

    def test_completed_recovery(self):
        row = shock_events(self.samples(), product="BTC-USD")[0]
        self.assertEqual(row["first_recovery_delay_ms"], 1000)
        self.assertEqual(row["available_at_ms"], row["shock_receiver_ms"] + 30000)

    def test_censored(self):
        row = shock_events(self.samples(False), product="BTC-USD")[0]
        self.assertTrue(row["censored_at_30s"])
        self.assertIsNone(row["first_recovery_delay_ms"])

    def test_gap_rejected(self):
        s = self.samples()
        s[35]["time_ms"] += 1000
        self.assertEqual(shock_events(s, product="BTC-USD"), [])

    def test_bad_future_rejected(self):
        s = self.samples()
        s[42]["valid"] = False
        self.assertEqual(shock_events(s, product="BTC-USD"), [])

    def test_truncated_future_rejected(self):
        self.assertEqual(shock_events(self.samples()[:55], product="BTC-USD"), [])


class ActualSourceTests(unittest.TestCase):
    t = 1794642600000

    def test_depth_both(self):
        for p in ("BTC-USD", "ETH-USD"):
            with self.subTest(product=p):
                r = depth(p, self.t)
                self.assertEqual(len(r["row"]["depth"]["bids"]), 10)
                self.assertEqual(len(r["row"]["depth"]["asks"]), 10)
                self.assertFalse(r["oos_approved"])
                self.assertFalse(r["live_feed"])

    def test_recovery_visibility(self):
        for p in ("BTC-USD", "ETH-USD"):
            for t in (self.t, self.t - 300000):
                r = recovery(p, t)
                self.assertTrue(all(e["available_at_ms"] <= t for e in r["events"]))

    def test_unknown_product(self):
        with self.assertRaises(ValueError):
            depth("SOL-USD", self.t)

    def test_unknown_cutoff(self):
        with self.assertRaises(L2APIError):
            depth("BTC-USD", self.t + 100)

    def test_invalid_limit(self):
        for n in (-1, 0, 31):
            with self.subTest(n=n), self.assertRaises(L2APIError):
                recovery("BTC-USD", self.t, n)

    def test_tamper_pin(self):
        with tempfile.TemporaryDirectory() as t:
            dest = Path(t)
            src = Path(__file__).resolve().parents[1] / "backend/data/v34_l2_depth"
            (dest / "projection.json").write_bytes(
                (src / "projection.json").read_bytes() + b" "
            )
            (dest / "projection.sha256.json").write_bytes(
                (src / "projection.sha256.json").read_bytes()
            )
            with patch("v34_l2_api.OUT", dest), self.assertRaises(L2APIError):
                load_l2()

    def test_rehashed_l1_mismatch(self):
        with tempfile.TemporaryDirectory() as t:
            dest = Path(t)
            src = Path(__file__).resolve().parents[1] / "backend/data/v34_l2_depth"
            data = json.loads((src / "projection.json").read_text())
            data["products"]["BTC-USD"]["depth_snapshots"][1]["depth"]["bids"][0][
                "quantity"
            ] = "999"
            raw = (
                json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            (dest / "projection.json").write_bytes(raw)
            pin = json.loads((src / "projection.sha256.json").read_text())
            pin["projection_sha256"] = sha256(raw).hexdigest()
            (dest / "projection.sha256.json").write_text(json.dumps(pin))
            with patch("v34_l2_api.OUT", dest), self.assertRaises(L2APIError):
                load_l2()


if __name__ == "__main__":
    unittest.main()


class HTTPReadOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer

        from bridge import Handler

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.root = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def get(self, path):
        from urllib.error import HTTPError
        from urllib.request import urlopen

        try:
            with urlopen(self.root + path, timeout=5) as result:
                return result.status, json.loads(result.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def test_real_level_route(self):
        code, r = self.get("/api/v34/depth?product=BTC-USD&asof=1794642600000")
        self.assertEqual(code, 200)
        from v30_unified_api import series

        f = series("BTC-USD", "features", 1794642300000, 1794642700000)["rows"]
        matched = [x for x in f if x["decision_receive_utc_ms"] == 1794642600000][0]
        self.assertEqual(r["row"]["depth"]["bids"][0]["price"], matched["best_bid"])
        self.assertEqual(r["levels_each_side"], 10)

    def test_recovery_route_available_at(self):
        code, r = self.get(
            "/api/v34/recovery?product=BTC-USD&asof=1794642600000&limit=5"
        )
        self.assertEqual(code, 200)
        self.assertTrue(
            all(e["available_at_ms"] <= r["asof_receiver_ms"] for e in r["events"])
        )

    def test_wrong_product_and_future_time_fail(self):
        for url in (
            "/api/v34/depth?product=SOL-USD&asof=1794642600000",
            "/api/v34/recovery?product=BTC-USD&asof=1794642600001",
        ):
            self.assertNotEqual(self.get(url)[0], 200)

    def test_repeated_and_extra_parameter_rejected(self):
        for url in (
            "/api/v34/depth?product=BTC-USD&product=ETH-USD&asof=1794642600000",
            "/api/v34/depth?product=BTC-USD&asof=1794642600000&live=true",
            "/api/v34/recovery?product=BTC-USD&asof=1794642600000&limit=999",
        ):
            self.assertNotEqual(self.get(url)[0], 200)

    def test_not_routable_as_trade(self):
        self.assertEqual(self.get("/api/v34/order?product=BTC-USD&side=BUY")[0], 404)

    def test_corrupt_evidence_returns_no_price(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            source = Path(__file__).resolve().parents[1] / "backend/data/v34_l2_depth"
            (root / "projection.json").write_bytes(
                (source / "projection.json").read_bytes() + b"garbage"
            )
            (root / "projection.sha256.json").write_bytes(
                (source / "projection.sha256.json").read_bytes()
            )
            with patch("v34_l2_api.OUT", root):
                code, result = self.get(
                    "/api/v34/depth?product=BTC-USD&asof=1794642600000"
                )
                self.assertNotEqual(code, 200)
                self.assertNotIn("row", result)
