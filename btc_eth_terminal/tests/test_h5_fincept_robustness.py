import copy
import tempfile
import unittest
from pathlib import Path
import numpy as np
from h5_experiment_registry import create, append, sha, series_hash
from h5_fincept_robustness import evaluate
from test_h5_experiment_registry import plan


def fixture(path, failed=False):
    p = plan(10)
    split = dict(
        train_bars=120,
        test_bars=60,
        folds=3,
        purge_bars=10,
        embargo_bars=5,
        information_lookback_bars=3,
        label_delay_bars=2,
        bootstrap_block_bars=12,
    )
    p["split_policy_sha256"] = sha(split)
    timestamps = [100000 + i * 60000 for i in range(400)]
    rng = np.random.default_rng(72)
    matrix = (rng.normal(0, 0.001, (400, 10)) + np.arange(10) * 0.00001).tolist()
    ids = [t["trial_id"] for t in p["trials"]]
    series = {tid: [r[j] for r in matrix] for j, tid in enumerate(ids)}
    pin, head = create(path, p)
    for i, tid in enumerate(ids):
        head = append(path, pin, head, dict(event="START", trial_id=tid))
        body = dict(
            event="COMPLETE",
            trial_id=tid,
            return_series_sha256=series_hash(series[tid], timestamps, 60000),
            result_sha256=sha({"i": i}),
        )
        if i == 0 and failed:
            body = dict(event="FAIL", trial_id=tid, reason="MISSING_DATA")
        head = append(path, pin, head, body)
    payload = dict(
        schema="h5_registered_returns_v1",
        return_basis="REGULAR_NET_PORTFOLIO_RETURNS",
        timestamps=timestamps,
        interval_ms=60000,
        series=series,
        split_policy=split,
        source_kind="SYNTHETIC_TEST_ONLY",
    )
    return pin, head, payload


class Robustness(unittest.TestCase):
    def test_pinned_vendor_dsr_pbo_bootstrap_and_walk_forward(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "r.db"
            pin, head, x = fixture(path)
            r = evaluate(path, pin, head, x, fixture=True)
            self.assertTrue(r["witness_parity"])
            self.assertEqual(r["pbo"]["splits"], 6)
            self.assertTrue(r["monte_carlo"]["scalar_path_parity"])
            self.assertEqual(len(r["folds"]), 3)
            self.assertFalse(r["oos_approved"])
            self.assertEqual(r, evaluate(path, pin, head, x, fixture=True))
            with self.assertRaises(ValueError):
                evaluate(path, pin, head, x)

    def test_changed_series_and_incomplete_trials_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "r.db"
            pin, head, x = fixture(path)
            y = copy.deepcopy(x)
            y["series"][next(iter(y["series"]))][0] = 0.1
            with self.assertRaises(ValueError):
                evaluate(path, pin, head, y, fixture=True)
            y = copy.deepcopy(x)
            y["split_policy"]["purge_bars"] = 0
            with self.assertRaises(ValueError):
                evaluate(path, pin, head, y, fixture=True)
            path = Path(d) / "f.db"
            pin, head, x = fixture(path, True)
            with self.assertRaises(ValueError):
                evaluate(path, pin, head, x, fixture=True)
