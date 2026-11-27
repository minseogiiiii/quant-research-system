import unittest
from h12_portfolio_allocation import allocate


def e(a, b, c):
    return {"variance": {"BTC-USD": str(a), "ETH-USD": str(c)}, "covariance": str(b)}


class Allocation(unittest.TestCase):
    def test_diagonal_and_erc(self):
        r = allocate(e(".01", "0", ".04"))
        self.assertAlmostEqual(r["minimum_variance"]["weights"]["BTC-USD"], 0.8)
        self.assertAlmostEqual(r["risk_parity"]["weights"]["BTC-USD"], 2 / 3)
        self.assertAlmostEqual(
            r["risk_parity"]["risk_contribution_fraction"]["BTC-USD"], 0.5
        )
        self.assertLessEqual(
            r["minimum_variance"]["variance_per_grid"],
            r["equal_weight"]["variance_per_grid"],
        )
        self.assertFalse(r["execution_authorized"])

    def test_corner_and_negative_correlation(self):
        r = allocate(e(".01", ".015", ".04"))
        self.assertEqual(r["minimum_variance"]["weights"]["BTC-USD"], 1)
        r = allocate(e(".01", "-.01", ".04"))
        self.assertAlmostEqual(r["minimum_variance"]["weights"]["BTC-USD"], 5 / 7)
        self.assertAlmostEqual(
            r["risk_parity"]["risk_contribution_fraction"]["ETH-USD"], 0.5
        )

    def test_singular_or_invalid_rejected(self):
        for a, b, c in [
            ("0", "0", ".04"),
            (".01", ".02", ".04"),
            (".01", ".03", ".04"),
            ("NaN", "0", ".04"),
        ]:
            with self.assertRaises(ValueError):
                allocate(e(a, b, c))
