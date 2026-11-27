import unittest
from decimal import Decimal

from h9_portfolio import simulate
from h11_risk import POLICY
from test_h9_portfolio import fixture


class RiskTests(unittest.TestCase):
    def test_product_cap_and_baseline_unchanged(self):
        rows, sig, p = fixture()
        base = simulate(rows, sig, p, initial_cash="300")
        limited = simulate(rows, sig, p, initial_cash="300", risk_policy=POLICY)
        self.assertEqual(base, simulate(rows, sig, p, initial_cash="300"))
        self.assertEqual(limited["portfolios"][0]["counts"]["entries"], 0)
        self.assertEqual(
            limited["portfolios"][0]["risk"]["entry_rejections"]["risk_product_cap"], 3
        )

    def test_gross_cap_and_loss_latch_exits_allowed(self):
        rows, sig, p = fixture()
        policy = dict(
            POLICY,
            per_product_fraction="0.8",
            gross_fraction="0.9",
            max_loss_fraction="0.001",
            max_drawdown_fraction="0.001",
        )
        r = simulate(rows, sig, p, initial_cash="150", risk_policy=policy)
        a = r["portfolios"][0]
        self.assertEqual(a["counts"]["entries"], 1)
        self.assertEqual(a["counts"]["exits"], 1)
        self.assertEqual(a["risk"]["entry_latch"], "risk_loss_latch")
        self.assertEqual(Decimal(a["realized_net_quote"]), -2)
        # On the same path a looser loss limit allows gross-cap evaluation.
        x = simulate(
            rows,
            sig,
            p,
            initial_cash="150",
            risk_policy=dict(policy, max_loss_fraction="1", max_drawdown_fraction="1"),
        )
        self.assertEqual(
            x["portfolios"][0]["risk"]["entry_rejections"]["risk_gross_cap"], 1
        )

    def test_stale_and_invalid_policy(self):
        rows, sig, p = fixture()
        # Remove ETH updates after its initial snapshot; BTC remains fresh.
        for _seq, _t, w in rows[1:]:
            w["events"] = [e for e in w["events"] if e["product_id"] == "BTC-USD"]
        p["max_age_ms"] = 80
        r = simulate(
            rows,
            [sig[0]],
            p,
            initial_cash="1000",
            risk_policy=dict(POLICY, per_product_fraction="0.5", gross_fraction="0.8"),
        )
        self.assertEqual(
            r["portfolios"][0]["risk"]["entry_rejections"]["risk_stale"], 1
        )
        with self.assertRaises(ValueError):
            simulate(rows, [], p, risk_policy=dict(POLICY, gross_fraction="0.01"))
