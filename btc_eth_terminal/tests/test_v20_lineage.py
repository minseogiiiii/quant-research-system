"""v20 canonical semantics and source-parent protections. The large real replay
is run by the explicit v20 release verifier, not repeated in every unit test.
"""

import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
def _q14_load_after_path_setup_0():
    from canonical_maker_v20 import build
    from reconcile_v19_lineages import reconcile
    return build, reconcile

build, reconcile = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0


class CanonicalMakerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = json.loads(
            (ROOT / "P1_MAKER_QUEUE_SCENARIOS_v19.json").read_text()
        )
        cls.matched = json.loads((ROOT / "V20_LINEAGE_RECONCILIATION.json").read_text())
        cls.report_b = json.loads(
            (ROOT / "reference/v19_b/P1_MAKER_TAPE_ONE_HOUR_v19.json").read_text()
        )

    def test_reconciled_matrix_frozen(self):
        obj = build(self.matched, self.original)
        self.assertEqual(len(obj["records"]), 7)
        self.assertIsNone(obj["account_maker_fee_bps_verified"])
        self.assertFalse(obj["user_venue_order_acks_or_fills_verified"])
        self.assertTrue(
            all(
                x["certified_real_maker_fill_lower_bound_base"] == "0"
                for x in obj["records"]
            )
        )

    def test_traded_maker_side_only(self):
        x = build(self.matched, self.original)
        eth = next(z for z in x["records"] if z["id"] == "eth_depth_stress_80000")
        self.assertEqual(
            eth["time_windows_ms"]["30000"]["historical_maker_side_same_price_volume"],
            "0.12774597",
        )
        self.assertEqual(
            eth["time_windows_ms"]["30000"]["receiver_only_trade_volume_sensitivity"],
            "0.12774597",
        )

    def test_preanchor_event_gate_is_not_equivalent_to_receive_gate(self):
        x = build(self.matched, self.original)
        btc = next(z for z in x["records"] if z["id"] == "btc_buy_seq_150000")
        self.assertEqual(
            btc["time_windows_ms"]["30000"]["receiver_only_trade_volume_sensitivity"],
            "0.00296636",
        )
        self.assertEqual(
            btc["time_windows_ms"]["30000"]["historical_maker_side_same_price_volume"],
            "0.00174371",
        )

    def test_published_b_no_extra_observations(self):
        self.assertEqual(len(self.report_b["results"]), 5)
        self.assertEqual(
            sum(
                z["v19_b_included_under_upstream_taker_depth_gate"]
                for z in self.matched["rows"]
            ),
            5,
        )

    def test_source_hash_change_fails_before_replay(self):
        x = deepcopy(self.original)
        x["source_manifest_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "different source"):
            reconcile(
                ROOT / "backend/data/live_v16_one_hour/coinbase_btc_eth_soak_eE9SNH",
                x,
                self.report_b,
            )

    def test_anchor_shift_fails_before_tape_scan(self):
        a = deepcopy(self.original)
        a["results"][0]["anchor_sequence"] += 1
        with self.assertRaisesRegex(ValueError, "Shared quote anchor"):
            reconcile(
                ROOT / "backend/data/live_v16_one_hour/coinbase_btc_eth_soak_eE9SNH",
                a,
                self.report_b,
            )

    def test_canonical_rejects_altered_anchors(self):
        a = deepcopy(self.original)
        a["results"][0]["anchor_sequence"] += 1
        with self.assertRaisesRegex(ValueError, "Anchor divergence"):
            build(self.matched, a)

    def test_canonical_rejects_false_parity_status(self):
        x = deepcopy(self.matched)
        x["status"] = "UNVERIFIED"
        with self.assertRaisesRegex(ValueError, "partially reconciled"):
            build(x, self.original)


if __name__ == "__main__":
    unittest.main()
