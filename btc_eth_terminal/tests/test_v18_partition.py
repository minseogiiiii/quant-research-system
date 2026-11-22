"""Source chronology contract: future price labels cannot join earlier records."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def _q14_load_after_path_setup_0():
    from feature_label_partition import (
        FeatureLabelPartitionError,
        labels_visible_asof,
        partition,
    )

    return FeatureLabelPartitionError, labels_visible_asof, partition


FeatureLabelPartitionError, labels_visible_asof, partition = (
    _q14_load_after_path_setup_0()
)
del _q14_load_after_path_setup_0


class PartitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = json.loads(
            (ROOT / "P1_POST_QUOTE_MARKOUT_v18.json").read_bytes()
        )

    def test_source_frozen_report_must_be_only_future_labels(self):
        result = partition(self.original)
        self.assertEqual(len(result["asof_quote_observations"]), 7)
        self.assertEqual(len(result["future_only_labels"]), 21)
        for row in result["asof_quote_observations"]:
            self.assertNotIn("future_midprice", row)
            self.assertNotIn("side_signed_midprice_move_bps", row)
            self.assertNotIn("labels", row)
            self.assertGreater(row["available_asof_sequence"], row["signal_sequence"])
        self.assertTrue(result["no_future_label_in_asof_records"])
        self.assertTrue(result["no_orders"])

    def test_cannot_access_frozen_first_label_at_quote_time(self):
        result = partition(self.original)
        self.assertEqual(labels_visible_asof(result, 1013), [])
        first = labels_visible_asof(result, 1060)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["id"], "btc_buy_seq_1000")
        self.assertEqual(first[0]["horizon_ms"], 1000)

    def test_quote_was_rejected_by_existing_depth_is_not_trading_fill(self):
        result = partition(self.original)
        orig = {x["id"]: x for x in result["asof_quote_observations"]}
        self.assertEqual(
            orig["btc_depth_stress_30000"]["original_quote_status"],
            "REJECTED_INSUFFICIENT_BEST_LEVEL_DEPTH",
        )
        self.assertEqual(
            orig["eth_depth_stress_80000"]["original_quote_status"],
            "REJECTED_INSUFFICIENT_BEST_LEVEL_DEPTH",
        )

    def test_source_future_label_timestamp_before_quote_fails_closed(self):
        bad = copy.deepcopy(self.original)
        bad["results"][0]["labels"][0]["label_observed_at_sequence"] = 1013
        with self.assertRaises(FeatureLabelPartitionError):
            partition(bad)

    def test_forged_earlier_partition_label_cannot_be_joined(self):
        result = partition(self.original)
        result["future_only_labels"][0]["available_after_source_sequence"] = 1013
        with self.assertRaises(FeatureLabelPartitionError):
            labels_visible_asof(result, 999999)

    def test_unknown_asof_or_label_schema_field_fails(self):
        bad = copy.deepcopy(self.original)
        bad["results"][0]["synthetic_future_leak"] = "bad"
        with self.assertRaises(FeatureLabelPartitionError):
            partition(bad)
        bad = copy.deepcopy(self.original)
        bad["results"][0]["labels"][0]["hidden_signal"] = "not_allowed"
        with self.assertRaises(FeatureLabelPartitionError):
            partition(bad)

    def test_corrupted_status_or_enabled_orders_fails(self):
        for field, value in [
            ("status", "PROFITABLE"),
            ("orders_enabled", True),
            ("actual_order_fills_verified", True),
        ]:
            bad = copy.deepcopy(self.original)
            bad[field] = value
            with (
                self.subTest(field=field),
                self.assertRaises(FeatureLabelPartitionError),
            ):
                partition(bad)

    def test_non_integer_asof_sequence_rejected(self):
        data = partition(self.original)
        for x in (True, "1013", -1):
            with self.subTest(x=x), self.assertRaises(FeatureLabelPartitionError):
                labels_visible_asof(data, x)


if __name__ == "__main__":
    unittest.main()
