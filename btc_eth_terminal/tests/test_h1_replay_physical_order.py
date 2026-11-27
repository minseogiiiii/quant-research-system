import json
import tempfile
import unittest
from pathlib import Path

import pyarrow.parquet as pq
from f5_independent_clock import read_independent
from h1_f6_dataset_replay import replay_dataset
from h1_streaming_parquet import convert, digest
from h2_native_benchmark import generate
from h3_momentum_horizons import project
from independent_soak_replay import strict_json


class ReplayOrder(unittest.TestCase):
    def test_native_parquet_and_reversed_file(self):
        with tempfile.TemporaryDirectory() as d:
            r = Path(d)
            pin, *_ = generate(r / "raw", 2_000_000, levels=8)
            out = r / "derived"
            convert(r / "raw", pin, out, fixture=True)
            m = strict_json((r / "raw" / "manifest.json").read_bytes())
            a = project(read_independent(r / "raw", m), "BTC-USD")
            b = replay_dataset(out, pin, "BTC-USD", fixture=True, projector=project)
            self.assertEqual(a, b)
            path = out / "packets.parquet"
            table = pq.read_table(path)
            pq.write_table(table.take(list(range(len(table) - 1, -1, -1))), path)
            # Even a rewritten file digest cannot authorize reversed sequence.
            receipt = out / "dataset.json"
            v = json.loads(receipt.read_bytes())
            v["tables"]["packets"]["file_sha256"] = digest(path)
            receipt.write_text(json.dumps(v))
            with self.assertRaisesRegex(ValueError, "H1_F6_PACKET"):
                replay_dataset(out, pin, "BTC-USD", fixture=True, projector=project)
