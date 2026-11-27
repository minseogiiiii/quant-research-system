import unittest
from decimal import Decimal
from statistics import covariance, stdev, correlation
from h11_covariance import RollingCovariance, estimate, size_units, POLICY
from h11_risk import POLICY as RISK
from h9_portfolio import simulate
from f4_depth_buckets import SCALE
from test_h9_portfolio import fixture


class Covariance(unittest.TestCase):
    def test_sample_math_and_missing_reset(self):
        rows = [
            (Decimal(a), Decimal(b))
            for a, b in [
                (".01", ".02"),
                ("-.02", ".01"),
                (".03", "-.01"),
                (".01", ".04"),
            ]
        ]
        e = estimate(rows)
        x, y = map(list, zip(*rows))
        xf = list(map(float, x))
        yf = list(map(float, y))
        self.assertAlmostEqual(float(e["covariance"]), covariance(xf, yf), places=14)
        self.assertAlmostEqual(float(e["volatility"]["BTC-USD"]), stdev(xf), places=14)
        self.assertAlmostEqual(float(e["correlation"]), correlation(xf, yf), places=14)
        c = RollingCovariance(100, 2)
        for i, m in enumerate(
            [
                {"BTC-USD": 100, "ETH-USD": 100},
                {"BTC-USD": 101, "ETH-USD": 102},
                {"BTC-USD": 103, "ETH-USD": 101},
            ]
        ):
            got = c.observe(1000 + i * 100, m)
        self.assertIsNotNone(got)
        self.assertIsNone(c.observe(1300, {}))
        self.assertIsNone(c.observe(1400, {"BTC-USD": 104, "ETH-USD": 103}))
        self.assertIsNone(c.observe(1500, {"BTC-USD": 105, "ETH-USD": 104}))
        self.assertIsNotNone(c.observe(1600, {"BTC-USD": 106, "ETH-USD": 102}))
        self.assertEqual(c.receipt()["ready_estimates"], 2)

    def test_inverse_volatility_round_down_and_max(self):
        e = {"volatility": {"BTC-USD": Decimal(".01"), "ETH-USD": Decimal(".02")}}
        p = dict(
            POLICY,
            risk_budget_fraction=".001",
            max_notional_fraction=".5",
            base_increment=".01",
        )
        a = size_units(e, "BTC-USD", 200 * SCALE, Decimal(1000), 10 * SCALE, p)
        b = size_units(e, "ETH-USD", 200 * SCALE, Decimal(1000), 10 * SCALE, p)
        self.assertEqual(a, SCALE)
        self.assertEqual(b, SCALE // 2)
        self.assertEqual(
            size_units(e, "BTC-USD", 200 * SCALE, Decimal(1000), SCALE // 3, p),
            33 * SCALE // 100,
        )

    def test_sizing_warmup_and_frozen_exit_inventory(self):
        rows, sig, p = fixture()
        risk = dict(RISK, per_product_fraction=".8", gross_fraction=".9")
        s = dict(
            POLICY,
            window_returns=2,
            risk_budget_fraction=".001",
            max_notional_fraction=".05",
        )
        # First signal has only one valid paired mark. A later signal
        # at 1300 has a complete window and can enter.
        late = [sig[0], (1300, "BTC-USD", 3, True)]
        rows.append((6, 1550, rows[-1][2]))
        r = simulate(
            rows, late, p, initial_cash="1000", risk_policy=risk, sizing_policy=s
        )
        a = r["portfolios"][0]
        self.assertEqual(a["sizing"]["unavailable"], 1)
        self.assertEqual(a["counts"]["entries"], 1)
        self.assertEqual(a["counts"]["exits"], 1)
        self.assertEqual(a["sizing"]["min_base_units"], SCALE // 2)
        self.assertEqual(Decimal(a["realized_net_quote"]), -1)
        self.assertTrue(a["cash_inventory_witness"])
        with self.assertRaises(ValueError):
            simulate(rows, late, p, sizing_policy=s)

    def test_zero_variance_undefined_correlation(self):
        e = estimate([(Decimal(0), Decimal(0))] * 3)
        self.assertIsNone(e["correlation"])
