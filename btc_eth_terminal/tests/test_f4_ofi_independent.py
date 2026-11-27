import copy
import tempfile
import unittest
from pathlib import Path

from f4_ofi_independent import audit
from f4_ofi_markout import build
from f4_order_flow import read_packets
from independent_soak_replay import strict_json
from test_f4_depth_buckets_archive import BASE
from test_f4_order_flow_archive import source


class Witness(unittest.TestCase):
    def test_separate_reference_and_mutations(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pin, _ = source(root)
            r = build(
                root,
                pin,
                product="BTC-USD",
                start_ms=BASE + 1000,
                end_ms=BASE + 4000,
                step_ms=1000,
                window_ms=500,
                max_age_ms=1000,
                horizons_ms=[100],
                tolerance_ms=1000,
            )
            m = strict_json((root / "manifest.json").read_bytes())

            def check(x):
                return audit(read_packets(root, m), x)

            self.assertTrue(check(r)["independent_software_parity"])
            for field in ("ofi_base", "microprice", "source_sequence"):
                bad = copy.deepcopy(r)
                bad["features"][0][field] = "999"
                with self.assertRaises(ValueError):
                    check(bad)
            bad = copy.deepcopy(r)
            bad["future_labels"][0]["label_available_ms"] += 1
            with self.assertRaises(ValueError):
                check(bad)
