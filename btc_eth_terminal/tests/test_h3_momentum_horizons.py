import copy
import unittest
from decimal import Decimal
from h3_momentum_horizons import project, POLICY
from test_f4_ofi_markout import packet


class Horizon(unittest.TestCase):
    def setup(self):
        p = copy.deepcopy(POLICY)
        p.update(
            grid_ms=100,
            momentum_ms=200,
            ofi_ms=200,
            max_age_ms=1000,
            latency_ms=10,
            tolerance_ms=100,
            horizons_ms=[100, 300, 900],
            momentum_bps="0.1",
            imbalance_min="0.1",
            capacity_fraction="1",
            fee_bps="0",
            extra_slippage_bps="0",
        )
        rows = [
            packet(0, 1000, "snapshot", [("bid", "99", "10"), ("offer", "101", "1")])
        ]
        for t in range(1050, 1851, 100):
            up = [("bid", "99", "11"), ("offer", "101", "1")]
            if t >= 1250:
                up = [("bid", "99", "0"), ("bid", "100", "20"), ("offer", "101", "1")]
            rows.append(packet(len(rows), t, "update", up))
        return rows, p

    def test_nested_cohorts_and_censoring(self):
        rows, p = self.setup()
        r = project(rows, "BTC-USD", p)
        self.assertGreater(r["momentum_signals"], 0)
        self.assertGreater(r["micro_filtered_signals"], 0)
        for h in r["results"]:
            a, b = h["cohorts"].values()
            self.assertLessEqual(b["opportunities"], a["opportunities"])
            self.assertEqual(
                a["opportunities"],
                a["completed"]
                + a["entry_rejected"]
                + a["exit_rejected"]
                + a["source_ended"],
            )
        self.assertEqual(r["results"][-1]["cohorts"]["momentum"]["completed"], 0)
        self.assertIsNone(
            r["results"][-1]["cohorts"]["momentum"]["mean_indicative_net_bps"]
        )
        self.assertFalse(r["portfolio_backtest"])

    def test_no_future_warmup_and_nonpositive_net(self):
        rows, p = self.setup()
        r = project(rows[:2], "BTC-USD", p)
        self.assertEqual(r["eligible_decisions"], 0)
        self.assertEqual(r["momentum_signals"], 0)
        self.assertFalse(r["alpha_confirmed"])

    def test_sequence_regression_rejected(self):
        rows, p = self.setup()
        rows[2] = (99, rows[2][1], rows[2][2])
        with self.assertRaises(ValueError):
            project(rows, "BTC-USD", p)
