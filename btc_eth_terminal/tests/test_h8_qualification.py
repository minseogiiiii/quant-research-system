import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from h1_streaming_parquet import convert
from h2_native_benchmark import generate
from h6_registered_strategy_runner import run
from h5_experiment_registry import sha
from h8_qualification import assess


class Qualification(unittest.TestCase):
    def test_real_engine_synthetic_never_approved_and_tampering(self):
        with tempfile.TemporaryDirectory() as d:
            r = Path(d)
            pin, *_ = generate(r / "raw", 2_000_000, levels=8)
            out = r / "dataset"
            convert(r / "raw", pin, out, fixture=True)
            entry = dict(
                dataset_path=str(out),
                dataset_receipt_sha256=hashlib.sha256(
                    (out / "dataset.json").read_bytes()
                ).hexdigest(),
                role="DEVELOPMENT",
            )
            journal = r / "trial.db"
            report = run(
                [entry],
                0,
                [{"product": "BTC-USD", "hold_ms": 5000}],
                42,
                journal,
                fixture=True,
            )
            args = (journal, report["plan_sha256"], report["registry_head_sha256"])
            x = assess(report, *args, sha(report))
            self.assertEqual(x["status"], "NO_APPROVAL")
            self.assertEqual(x["trials"][0]["verdict"], "BLOCKED")
            self.assertIn("SYNTHETIC_ONLY", x["trials"][0]["reasons"])
            self.assertFalse(x["shadow_authorized"])
            with self.assertRaises(ValueError):
                assess(report, *args, "0" * 64)
            y = copy.deepcopy(report)
            y["results"][0]["mean_session_net_return"] = 1.0
            with self.assertRaises(ValueError):
                assess(y, *args, sha(y))
            y = copy.deepcopy(report)
            y["alpha_confirmed"] = True
            with self.assertRaises(ValueError):
                assess(y, *args, sha(y))
