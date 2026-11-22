"""No selection of a best hypothetical scenario or market order from replay."""

from __future__ import annotations

import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from taker_sensitivity import SensitivityError, compare

INTENTS = json.loads((ROOT / "examples/v15_demo_intents.json").read_text())
BASE = {
    "id": "baseline",
    "latency_ms": 250,
    "max_wait_ms": 5000,
    "max_signal_quote_age_ms": 2500,
    "one_way_taker_fee_bps": "10",
    "extra_slippage_bps": "5",
    "top_level_capacity_fraction": "0.25",
}
STRESS = {
    **BASE,
    "id": "higher_fee_slippage",
    "one_way_taker_fee_bps": "20",
    "extra_slippage_bps": "15",
}


class ScenarioGuardTests(unittest.TestCase):
    def test_conflicting_or_missing_scenario_fields_rejected_before_any_market_read(
        self,
    ):
        bad = [BASE, {**BASE, "id": "baseline"}]
        with patch("taker_sensitivity.evaluate") as spy:
            with self.assertRaises(SensitivityError):
                compare(Path("/not/a/market"), INTENTS, bad)
            spy.assert_not_called()
        bad = [BASE, {**STRESS, "extra_slippage_bps": "NaN"}]
        with patch("taker_sensitivity.evaluate") as spy:
            with self.assertRaises(SensitivityError):
                compare(Path("/not/a/market"), INTENTS, bad)
            spy.assert_not_called()

    def test_first_must_be_baseline_and_no_unbounded_parameter_search(self):
        with self.assertRaises(SensitivityError):
            compare(Path("/missing"), INTENTS, [STRESS, BASE])
        with self.assertRaises(SensitivityError):
            compare(
                Path("/missing"),
                INTENTS,
                [BASE] + [dict(STRESS, id=f"x_{i}") for i in range(6)],
            )

    def test_user_cannot_inject_strategies_or_api_write_in_scenarios(self):
        with self.assertRaises(SensitivityError):
            compare(Path("/missing"), INTENTS, [BASE, {**STRESS, "api_key": "secret"}])


class RealArchiveSensitivity(unittest.TestCase):
    def test_cost_increase_reduces_modeled_cashflow_not_routed_fills(self):
        source = ROOT / "backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy"
        r = compare(source, INTENTS, [BASE, STRESS])
        self.assertEqual(r["scenario_count"], 2)
        self.assertEqual(r["observation_count"], 3)
        self.assertFalse(r["best_scenario_selected"])
        self.assertFalse(r["actual_order_sent"])
        self.assertFalse(r["actual_fills_verified"])
        baseline = r["scenario_matrix"][0]["observations"]
        stress = r["scenario_matrix"][1]["observations"]
        for a, b in zip(baseline, stress):
            self.assertEqual(a["id"], b["id"])
            self.assertEqual(a["quote_sequence"], b["quote_sequence"])
            self.assertEqual(a["status"], "INDICATIVE_TAKER_QUOTE_ONLY")
            self.assertEqual(b["status"], "INDICATIVE_TAKER_QUOTE_ONLY")
            self.assertLess(
                Decimal(b["hypothetical_cashflow_usd"]),
                Decimal(a["hypothetical_cashflow_usd"]),
            )


if __name__ == "__main__":
    unittest.main()
