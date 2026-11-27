import tempfile
import unittest
from pathlib import Path
from h2_native_benchmark import generate
from h1_streaming_parquet import convert


class Benchmark(unittest.TestCase):
    def test_small_generated_native_archive(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            raw = root / "raw"
            pin, size, count, chunks = generate(raw, 150000, levels=8)
            a = convert(raw, pin, root / "dataset", fixture=True)
            self.assertGreaterEqual(size, 150000)
            self.assertEqual(a["tables"]["l2"]["rows"], count * 32)
            self.assertEqual(a["tables"]["packets"]["rows"], count)
            self.assertTrue(a["independent_replay_parity"])
            self.assertFalse(a["research_eligible"])
