import hashlib
import tempfile
import unittest
from pathlib import Path
from h2_native_benchmark import generate
from h1_streaming_parquet import convert
from h6_registered_strategy_runner import run, prepare
from h5_experiment_registry import snapshot


class Runner(unittest.TestCase):
    def test_real_code_path_fixture_and_registry(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pin, *_ = generate(root / "raw", 2000000, levels=8)
            convert(root / "raw", pin, root / "dataset", fixture=True)
            entry = {
                "dataset_path": str(root / "dataset"),
                "dataset_receipt_sha256": hashlib.sha256(
                    (root / "dataset" / "dataset.json").read_bytes()
                ).hexdigest(),
                "role": "DEVELOPMENT",
            }
            trials = [
                {"product": "BTC-USD", "hold_ms": 5000},
                {"product": "BTC-USD", "hold_ms": 10000},
            ]
            r = run([entry], 0, trials, 42, root / "journal.db", fixture=True)
            self.assertTrue(r["all_complete"])
            self.assertEqual(len(r["results"]), 2)
            self.assertFalse(r["oos_approved"])
            self.assertFalse(r["selection_performed"])
            self.assertTrue(
                snapshot(
                    root / "journal.db", r["plan_sha256"], r["registry_head_sha256"]
                )["all_complete"]
            )
            with self.assertRaises(ValueError):
                prepare(
                    [dict(entry, role="DECLARED_HOLDOUT")], 0, trials, 42, fixture=True
                )

    def test_invalid_policy_rejected(self):
        from h6_registered_strategy_runner import policy_for

        with self.assertRaises(ValueError):
            policy_for({"product": "BTC-USD", "hold_ms": 1})
