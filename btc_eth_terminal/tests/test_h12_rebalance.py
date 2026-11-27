import unittest
from decimal import Decimal
from h12_rebalance import shrink, weights, project, POLICY
from h11_covariance import estimate


class Rebalance(unittest.TestCase):
    def test_diagonal_shrinkage_and_degeneracy(self):
        e = estimate(
            [(Decimal(".01"), Decimal(".02")), (Decimal("-.01"), Decimal("-.02"))]
        )
        with self.assertRaises(ValueError):
            shrink(e, Decimal(0))
        a = shrink(e, Decimal(".25"))
        self.assertEqual(a["variance"], e["variance"])
        self.assertAlmostEqual(float(a["covariance"] / e["covariance"]), 0.75)
        self.assertAlmostEqual(float(sum(weights(a, "minimum_variance").values())), 1)

    def test_event_rebalance_and_cost_rejection(self):
        rows = []
        for i in range(21):
            t = 1050 + i * 100
            events = []
            for p, base in (("BTC-USD", 100), ("ETH-USD", 200)):
                # Remove previous levels before replacing them.
                px = base + (i % 3 if p == "BTC-USD" else (i * i) % 5)
                old = base + ((i - 1) % 3 if p == "BTC-USD" else ((i - 1) ** 2) % 5)
                updates = []
                if i and old != px:
                    updates.extend(
                        dict(side=s, price_level=str(v), new_quantity="0")
                        for s, v in (("bid", old), ("offer", old + 1))
                    )
                updates.extend(
                    dict(side=s, price_level=str(v), new_quantity="1000")
                    for s, v in (("bid", px), ("offer", px + 1))
                )
                events.append(
                    dict(
                        type="snapshot" if i == 0 else "update",
                        product_id=p,
                        updates=updates,
                    )
                )
            rows.append((i, t, dict(channel="l2_data", events=events)))
        p = dict(
            POLICY,
            grid_ms=100,
            window_returns=2,
            max_age_ms=100,
            latency_ms=10,
            tolerance_ms=100,
            shrinkage=".25",
            max_cost_bps="100",
            fee_bps="10",
            deadband_fraction="0",
        )
        r = project(rows, p)
        self.assertGreater(r["counts"]["fills"], 0)
        self.assertGreater(Decimal(r["trading_cost_quote"]), 0)
        self.assertTrue(r["cash_inventory_witness"])
        self.assertFalse(r["execution_authorized"])
        rejected = project(rows, dict(p, max_cost_bps="0"))
        self.assertEqual(rejected["counts"]["fills"], 0)
        self.assertGreater(rejected["counts"]["decision_cost_rejected"], 0)
        self.assertEqual(Decimal(rejected["ending_cash"]), 10000)
