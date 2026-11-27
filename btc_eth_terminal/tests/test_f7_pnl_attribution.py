import copy
import unittest
from decimal import Decimal

from f5_execution_depth import SCALE, walk
from f6_liquidity_recovery import POLICY, project
from f7_pnl_attribution import decompose, summarize
from test_f4_ofi_markout import packet


class Attribution(unittest.TestCase):
    def test_exact_cost_waterfall(self):
        b = {99 * SCALE: 10 * SCALE}
        a = {101 * SCALE: SCALE, 102 * SCALE: 2 * SCALE}
        x = walk(b, a, "BUY", 2 * SCALE, SCALE, 10 * SCALE, 2 * SCALE)
        y = walk(
            {103 * SCALE: 10 * SCALE},
            {105 * SCALE: 10 * SCALE},
            "SELL",
            2 * SCALE,
            SCALE,
            10 * SCALE,
            2 * SCALE,
        )
        z = decompose({"quote": x}, {"quote": y}, "100", "104")
        self.assertEqual(Decimal(z["arrival_mid_gross_quote"]), 8)
        self.assertEqual(Decimal(z["spread_cost"]), 4)
        self.assertEqual(Decimal(z["depth_cost"]), 1)
        self.assertEqual(
            Decimal(z["net_quote"]),
            Decimal(y["all_in_quote"]) - Decimal(x["all_in_quote"]),
        )
        self.assertEqual(Decimal(z["entry_latency_pnl"]), 0)

    def test_trace_does_not_change_policy_and_ledger_reconciles(self):
        p = copy.deepcopy(POLICY)
        p.update(
            warmup_samples=2,
            step_ms=100,
            hold_ms=100,
            latency_ms=10,
            tolerance_ms=100,
            max_age_ms=1000,
            fee_bps="0",
            extra_slippage_bps="0",
            capacity_fraction="1",
        )
        rows = [
            packet(0, 1000, "snapshot", [("bid", "99", "10"), ("offer", "101", "1")])
        ]
        for t, q in [
            (1050, "10"),
            (1150, "10"),
            (1250, "4"),
            (1350, "8"),
            (1450, "8"),
            (1550, "8"),
            (1650, "8"),
            (1750, "8"),
        ]:
            rows.append(packet(len(rows), t, "update", [("bid", "99", q)]))
        r, a = summarize(rows, "BTC-USD", p)
        self.assertEqual(r, project(rows, "BTC-USD", p))
        self.assertEqual(Decimal(a["totals"]["net_quote"]), Decimal("-0.02"))
        self.assertEqual(Decimal(a["totals"]["arrival_mid_gross_quote"]), 0)
        self.assertEqual(Decimal(a["totals"]["spread_cost"]), Decimal("0.02"))
        self.assertTrue(a["final_flat_cash_reconciled"])
        self.assertTrue(a["complete_regular_grid"])
        self.assertEqual(len(a["returns"]), a["grid_points"] - 1)

    def test_missing_exit_mid_not_invented(self):
        x = walk({99 * SCALE: SCALE}, {101 * SCALE: SCALE}, "BUY", SCALE, SCALE, 0, 0)
        y = walk({99 * SCALE: SCALE}, {101 * SCALE: SCALE}, "SELL", SCALE, SCALE, 0, 0)
        z = decompose({"quote": x}, {"quote": y}, "100")
        self.assertIsNone(z["exit_latency_pnl"])
        self.assertIsNone(z["decision_mid_gross_quote"])
