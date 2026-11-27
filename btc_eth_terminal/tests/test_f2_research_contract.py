"""F2 qualification against pinned upstream and existing metrics definitions."""

import math
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor" / "fincept"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT.parent / "src"))
def _load_after_path_setup():
    from base import metrics, robustness
    from f2_research_contract import (
        Event,
        chronological_split,
        metric_witness,
        psr_reference,
        qualified_pbo,
        regular_returns,
    )
    from world_quant_system.backtest.metrics import _sharpe_ratio

    return (metrics, robustness, Event, chronological_split, metric_witness,
            psr_reference, qualified_pbo, regular_returns, _sharpe_ratio)


(metrics, robustness, Event, chronological_split, metric_witness, psr_reference,
 qualified_pbo, regular_returns, _sharpe_ratio) = _load_after_path_setup()
del _load_after_path_setup


class ResearchContracts(unittest.TestCase):
    def test_explicit_ddof_bridge(self):
        r = np.tile([0.01, -0.02, 0.03, 0.005, -0.008], 10)
        times = [1_700_000_000_000 + i * 86400000 for i in range(len(r))]
        out = metric_witness(r, times, 86400000)
        self.assertAlmostEqual(out["sample_sharpe"], metrics.sharpe_ratio(r, 0.0, 365))
        self.assertAlmostEqual(out["population_sharpe"], _sharpe_ratio(tuple(r), 365))
        self.assertAlmostEqual(
            out["sample_sharpe"],
            out["population_sharpe"] * math.sqrt((len(r) - 1) / len(r)),
        )
        self.assertFalse(out["alpha_confirmed"])
        self.assertIsNone(metric_witness(r[:5], times[:5], 86400000)["sample_sharpe"])

    def test_gap_and_nonfinite_rejected(self):
        for values, times in (
            ([0.1, float("nan")], [100, 110]),
            ([0.1, 0.2], [100, 120]),
            ([0.1, 0.2], [100, 100]),
            ([0.1, -1.0], [100, 110]),
        ):
            with self.assertRaises(ValueError):
                regular_returns(values, times, 10)

    def test_event_time_purge_and_label_maturity(self):
        events = [
            Event(100, 99, 110, 115, "a"),
            Event(160, 150, 180, 190, "a"),
            Event(190, 180, 210, 215, "a"),
            Event(210, 205, 230, 235, "b"),
            Event(290, 285, 310, 315, "b"),
        ]
        result = chronological_split(events, 200, 300, 15)
        self.assertEqual(result["train"], [0])
        self.assertEqual(result["test"], [3])
        self.assertEqual(result["purged"], [1, 2, 4])
        with self.assertRaises(ValueError):
            chronological_split([Event(100, 101, 110, 115, "a")], 200, 300, 0)

    def test_psr_and_dsr_reference(self):
        for sr, hurdle, n, skew, kurt in (
            (0.08, 0.0, 100, 0.0, 3.0),
            (0.15, 0.1, 500, -0.4, 5.0),
        ):
            self.assertAlmostEqual(
                psr_reference(sr, hurdle, n, skew, kurt),
                robustness.probabilistic_sharpe(sr, hurdle, n, skew, kurt),
            )
        r = np.tile([0.01, -0.02, 0.03, -0.01, 0.005], 50)
        trials = np.array([-0.05, 0.01, 0.04, 0.12])
        out = robustness.deflated_sharpe(r, trials)
        sr = np.mean(r) / np.std(r, ddof=1)
        self.assertAlmostEqual(out["bestSharpePerBar"], sr)
        expected = psr_reference(
            sr,
            out["expectedMaxSharpePerBar"],
            len(r),
            metrics.skewness(r) or 0.0,
            (metrics.excess_kurtosis(r) or 0.0) + 3.0,
        )
        self.assertAlmostEqual(out["deflatedSharpe"], expected)

    def test_pbo_missing_trial_matrix_rejected(self):
        rng = np.random.default_rng(7)
        r = rng.normal(0, 0.01, (240, 12))
        times = [1000 + 60 * i for i in range(240)]
        out = qualified_pbo(r, times, 60, robustness.pbo_cscv)
        self.assertIsNotNone(out["pbo"])
        r[1, 2] = np.nan
        with self.assertRaises(ValueError):
            qualified_pbo(r, times, 60, robustness.pbo_cscv)


if __name__ == "__main__":
    unittest.main()
