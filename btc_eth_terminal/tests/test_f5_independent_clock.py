import tempfile
import unittest
from pathlib import Path
from f5_independent_clock import milliseconds,read_independent
from f4_order_flow import read_packets
from independent_soak_replay import strict_json
from test_f4_order_flow_archive import source

class Clock(unittest.TestCase):
    def test_integer_epoch(self):
        self.assertEqual(milliseconds("1970-01-01T00:00:00.999999999Z"),999)
        self.assertEqual(milliseconds("2000-02-29T00:00:00.001Z"),951782400001)
        for s in ("2001-02-29T00:00:00Z","2000-01-01T24:00:00Z","2000-01-01T00:00:60Z","2000-01-01T00:00:00+00:00"):
            with self.assertRaises(ValueError):milliseconds(s)
    def test_archive_reader(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source(root)
            m=strict_json((root/"manifest.json").read_bytes())
            self.assertEqual(list(read_packets(root,m)),list(read_independent(root,m)))
