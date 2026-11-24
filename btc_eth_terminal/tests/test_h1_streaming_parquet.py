import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from test_f4_order_flow_archive import source
from h1_streaming_parquet import convert
from v36_3_durable_archive_gate import audit

def fixture(root):
    source(root)
    m=json.loads((root/"manifest.json").read_bytes())
    m.update(synthetic_fixture=True,required_duration_seconds=15,finish_reason="DURATION_REACHED",total_reconnects=0)
    raw=json.dumps(m).encode();(root/"manifest.json").write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()

class Streaming(unittest.TestCase):
    def test_parquet_roundtrip_and_fixture_not_promoted(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d)/"raw";r.mkdir();pin=fixture(r)
            out=Path(d)/"dataset"
            result=convert(r,pin,out,fixture=True)
            self.assertTrue(result["independent_replay_parity"])
            self.assertFalse(result["research_eligible"])
            self.assertEqual(result["tables"]["packets"]["rows"],10)
            self.assertTrue((out/"l2.parquet").is_file())
            with self.assertRaises(ValueError):convert(r,pin,Path(d)/"real")
            with self.assertRaises(ValueError):convert(r,pin,out,fixture=True)
    def test_tamper_never_publishes(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d)/"raw";r.mkdir();pin=fixture(r)
            c=next((r/"chunks").glob("*.jsonl"));c.write_bytes(c.read_bytes()+b"x")
            out=Path(d)/"dataset"
            with self.assertRaises(ValueError):convert(r,pin,out,fixture=True)
            self.assertFalse(out.exists())
