"""Independent rederivation against identical original Coinbase archived source."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
def _q14_load_after_path_setup_0():
    from decimal import Decimal
    from v23_independent_microstructure_audit import (
        IndependentOFIReject,
        independent_delta,
        independent_verify,
    )
    return Decimal, IndependentOFIReject, independent_delta, independent_verify

Decimal, IndependentOFIReject, independent_delta, independent_verify = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0


class IndependentOFITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (
            ROOT / "backend/data/live_v16_one_hour/coinbase_btc_eth_soak_eE9SNH"
        )
        cls.report = json.loads(
            (ROOT / "V23_CAUSAL_MICROSTRUCTURE_DIAGNOSTIC.json").read_text()
        )

    def test_reproduce_all_24_real_receiver_asof_rows(self):
        result = independent_verify(self.source, self.report)
        self.assertEqual(result["decision_rows_checked"], 24)
        self.assertEqual(result["source_message_count"], 152069)
        self.assertFalse(result["alpha_or_fills_verified"])
        self.assertEqual(
            result,
            json.loads(
                (ROOT / "V23_INDEPENDENT_MICROSTRUCTURE_AUDIT.json").read_text()
            ),
        )

    def test_ofi_arithmetic_direct(self):
        a = (Decimal(100), Decimal(10), Decimal(101), Decimal(6))
        b = (Decimal(100), Decimal(12), Decimal(101), Decimal(4))
        self.assertEqual(independent_delta(a, b), 4)

    def test_source_provenance_poison(self):
        bad = copy.deepcopy(self.report)
        bad["source_manifest_sha256"] = "0" * 64
        with self.assertRaisesRegex(IndependentOFIReject, "manifest identity"):
            independent_verify(self.source, bad)

    def test_same_raw_price_but_fake_ofi_detected(self):
        bad = copy.deepcopy(self.report)
        bad["results"][0]["ofi_rolling_5m_base"] = "999999"
        with self.assertRaisesRegex(IndependentOFIReject, "ofi_rolling_5m_base"):
            independent_verify(self.source, bad)

    def test_reordered_references_rejected(self):
        bad = copy.deepcopy(self.report)
        bad["results"][0], bad["results"][1] = bad["results"][1], bad["results"][0]
        with self.assertRaisesRegex(IndependentOFIReject, "instrument/clock"):
            independent_verify(self.source, bad)

    def test_missing_row_rejected(self):
        bad = copy.deepcopy(self.report)
        bad["results"].pop()
        with self.assertRaisesRegex(IndependentOFIReject, "sample count"):
            independent_verify(self.source, bad)


if __name__ == "__main__":
    unittest.main()
