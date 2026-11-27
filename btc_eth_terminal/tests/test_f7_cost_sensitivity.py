import unittest
from decimal import Decimal
from f7_cost_sensitivity import sensitivity, economics


class CostSensitivity(unittest.TestCase):
    def test_exact_counterfactual_and_break_even(self):
        entry, exit = Decimal(100), Decimal(101)
        baseline, fee, slip = economics(entry, exit, "10", "2")
        r = {
            "schema": "f7_attribution_v1",
            "alpha_confirmed": False,
            "execution_authorized": False,
            "oos_approved": False,
            "completed_roundtrips": 1,
            "unresolved_position": False,
            "totals": {
                "entry_depth_notional": "100",
                "exit_depth_notional": "101",
                "net_quote": str(baseline),
                "fee_cost": str(fee),
                "extra_slippage_cost": str(slip),
            },
        }
        s = sensitivity(r, {"fee_bps": "10", "extra_slippage_bps": "2"})
        self.assertEqual(len(s["scenario_rows"]), 28)
        self.assertEqual(Decimal(s["scenario_rows"][0]["net_quote"]), 1)
        self.assertAlmostEqual(
            float(s["break_even"][0]["break_even_fee_bps_signed"]), 10000 / 201
        )
        self.assertFalse(s["scenario_selected"])

    def test_empty_is_not_profitable(self):
        r = {
            "schema": "f7_attribution_v1",
            "alpha_confirmed": False,
            "execution_authorized": False,
            "oos_approved": False,
            "completed_roundtrips": 0,
            "unresolved_position": False,
            "totals": {
                k: "0"
                for k in (
                    "entry_depth_notional",
                    "exit_depth_notional",
                    "net_quote",
                    "fee_cost",
                    "extra_slippage_cost",
                )
            },
        }
        s = sensitivity(r, {"fee_bps": "10", "extra_slippage_bps": "2"})
        self.assertIsNone(s["scenario_rows"][0]["net_quote"])
        self.assertIsNone(s["break_even"][0]["break_even_fee_bps_signed"])

    def test_wrong_baseline_rejected(self):
        r = {
            "schema": "f7_attribution_v1",
            "alpha_confirmed": False,
            "execution_authorized": False,
            "oos_approved": False,
            "completed_roundtrips": 1,
            "unresolved_position": False,
            "totals": {
                "entry_depth_notional": "100",
                "exit_depth_notional": "101",
                "net_quote": "99",
                "fee_cost": "0",
                "extra_slippage_cost": "0",
            },
        }
        with self.assertRaises(ValueError):
            sensitivity(r, {"fee_bps": "0", "extra_slippage_bps": "0"})
