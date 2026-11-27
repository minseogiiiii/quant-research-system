"""Standalone qualification of unmodified Fincept algorithms.

Not a strategy backtest.
"""

import math
import sys
import unittest
from pathlib import Path

import numpy as np

VENDOR = Path(__file__).resolve().parents[1] / "vendor" / "fincept"
sys.path.insert(0, str(VENDOR))
from base import metrics, monte_carlo, robustness  # noqa: E402


class FinceptQualification(unittest.TestCase):
    def test_import_origin_and_metrics(self):
        for module in (metrics, monte_carlo, robustness):
            self.assertTrue(Path(module.__file__).resolve().is_relative_to(VENDOR))
        equity = np.array([100.0, 120.0, 90.0, 130.0])
        self.assertAlmostEqual(metrics.total_return(equity), 0.3)
        self.assertAlmostEqual(metrics.max_drawdown(equity), 0.25)
        r = np.array([0.01, -0.02, 0.03, 0.0, -0.01])
        ppy = 365.0  # explicit 24/7 daily grid; never inferred from weekday sample
        mean = sum(r) / len(r)
        sd = math.sqrt(sum((x - mean) ** 2 for x in r) / (len(r) - 1))
        self.assertAlmostEqual(
            metrics.sharpe_ratio(r, 0.0, ppy), mean / sd * math.sqrt(ppy)
        )
        why = {}
        self.assertIsNone(metrics.sharpe_ratio(np.zeros(5), why=why))
        self.assertIn("sharpeRatio", why)
        self.assertLessEqual(metrics.conditional_var(r), metrics.value_at_risk(r))

    def test_seeded_stationary_bootstrap(self):
        args = dict(n_sims=100, seed=7, initial=100.0, block_size=3.0, horizon=20)
        r = np.array([0.01, -0.02, 0.03, -0.01, 0.005])
        a = monte_carlo.simulate_paths("block_bootstrap", r, **args)
        b = monte_carlo.simulate_paths("block_bootstrap", r, **args)
        for key in ("finals", "mdds", "mins", "grid"):
            np.testing.assert_array_equal(a[key], b[key])
            self.assertTrue(np.isfinite(a[key]).all())
        self.assertTrue(np.all((a["mdds"] >= 0) & (a["mdds"] <= 1)))
        self.assertEqual(len(a["finals"]), 100)

    def test_shuffle_terminal_equity_invariant(self):
        r = np.array([0.01, -0.02, 0.03, -0.01, 0.005])
        out = monte_carlo.simulate_paths(
            "trade_shuffle", r, n_sims=100, seed=8, initial=100.0
        )
        target = 100.0 * math.prod(1 + v for v in r)
        np.testing.assert_allclose(out["finals"], target, rtol=1e-12)

    def test_purge_bar_fixture_only(self):
        folds = robustness.purged_kfold(100, n_folds=5, purge_bars=3, embargo_bars=2)
        for f in folds:
            a, b = f["test"]
            for start, end in f["train"]:
                self.assertTrue(end <= a - 3 or start >= b + 5)
        self.assertEqual(sum(f["testBars"] for f in folds), 100)

    def test_pbo_insufficient_samples_not_zero(self):
        result = robustness.pbo_cscv(np.ones((20, 3)))
        self.assertIsNone(result["pbo"])
        self.assertTrue(result["reason"])


if __name__ == "__main__":
    unittest.main()
