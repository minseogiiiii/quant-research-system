import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from h1_streaming_parquet import convert
from test_f4_order_flow_archive import source


def fixture(root):
    source(root)
    m = json.loads((root / "manifest.json").read_bytes())
    m.update(
        synthetic_fixture=True,
        required_duration_seconds=15,
        finish_reason="DURATION_REACHED",
        total_reconnects=0,
    )
    raw = json.dumps(m).encode()
    (root / "manifest.json").write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


class Streaming(unittest.TestCase):
    def test_parquet_roundtrip_and_fixture_not_promoted(self):
        with tempfile.TemporaryDirectory() as d:
            r = Path(d) / "raw"
            r.mkdir()
            pin = fixture(r)
            out = Path(d) / "dataset"
            result = convert(r, pin, out, fixture=True)
            self.assertTrue(result["independent_replay_parity"])
            self.assertFalse(result["research_eligible"])
            self.assertEqual(result["tables"]["packets"]["rows"], 10)
            self.assertTrue((out / "l2.parquet").is_file())
            with self.assertRaises(ValueError):
                convert(r, pin, Path(d) / "real")
            with self.assertRaises(ValueError):
                convert(r, pin, out, fixture=True)

    def test_tamper_never_publishes(self):
        with tempfile.TemporaryDirectory() as d:
            r = Path(d) / "raw"
            r.mkdir()
            pin = fixture(r)
            c = next((r / "chunks").glob("*.jsonl"))
            c.write_bytes(c.read_bytes() + b"x")
            out = Path(d) / "dataset"
            with self.assertRaises(ValueError):
                convert(r, pin, out, fixture=True)
            self.assertFalse(out.exists())

    def test_physical_order_and_canonical_parity(self):
        import pyarrow.parquet as pq
        from h1_streaming_parquet import verify_tables
        from q293_epoch_audit import scan

        with tempfile.TemporaryDirectory() as d:
            r = Path(d) / "raw"
            r.mkdir()
            pin = fixture(r)
            out = Path(d) / "dataset"
            timings = {}
            result = convert(r, pin, out, fixture=True, timings=timings)
            self.assertEqual(
                set(timings),
                {
                    "durable_admission_seconds",
                    "raw_epoch_replay_seconds",
                    "normalization_and_write_seconds",
                    "verify_packets_seconds",
                    "verify_l2_seconds",
                    "verify_trades_seconds",
                },
            )
            expected = {
                k: (v["rows"], v["canonical_rows_sha256"])
                for k, v in result["tables"].items()
            }
            p = out / "l2.parquet"
            data = pq.read_table(p)
            pq.write_table(data.take(list(range(len(data) - 1, -1, -1))), p)
            with self.assertRaisesRegex(ValueError, "PARQUET_PHYSICAL_ORDER"):
                verify_tables(out, expected, scan(r, pin, "primary"))
