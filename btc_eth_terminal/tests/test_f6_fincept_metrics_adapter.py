import unittest

from f6_fincept_metrics_adapter import evaluate


class Metrics(unittest.TestCase):
    def test_regular_net_grid(self):
        r = [0.01, -0.02, 0.03, 0.0, -0.01] * 6
        d = evaluate(r, [1000 * (i + 1) for i in range(len(r))], 1000)
        self.assertTrue(d["metric_parity"])
        self.assertGreater(d["max_drawdown"], 0)
        self.assertIsNotNone(d["annualized_sample_sharpe"])
        self.assertFalse(d["alpha_confirmed"])

    def test_missing_and_irregular_rejected(self):
        for r, t in [
            ([0, float("nan")], [1000, 2000]),
            ([0, 0], [1000, 3000]),
            ([0, -1], [1000, 2000]),
        ]:
            with self.assertRaises(ValueError):
                evaluate(r, t, 1000)

    def test_short_and_flat(self):
        self.assertIsNone(
            evaluate([0.01, -0.01], [1000, 2000], 1000)["annualized_sample_sharpe"]
        )
        self.assertIsNone(
            evaluate([0.0] * 20, [1000 * (i + 1) for i in range(20)], 1000)[
                "annualized_sample_sharpe"
            ]
        )
