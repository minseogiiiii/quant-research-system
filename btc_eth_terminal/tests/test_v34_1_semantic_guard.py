"""v34.1: independent semantic checks even when a malformed artifact is freshly re-pinned.

No original market archive, derived projection or original evidence pin is modified.
The test deliberately simulates a newly approved but internally inconsistent
projection by patching the immutable digest inside the isolated test process.
"""

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import v34_l2_api as l2

SOURCE = Path(__file__).resolve().parents[1] / "backend/data/v34_l2_depth"


class V341SemanticInvariantTests(unittest.TestCase):
    def setUp(self):
        self.original = json.loads((SOURCE / "projection.json").read_text())

    def test_original_still_admissible(self):
        data, digest, *_ = l2.load_l2()
        self.assertEqual(digest, l2.PINNED_L2_SHA256)
        self.assertFalse(data["trading_enabled"])

    def check_invalid_repin(self, edit):
        candidate = copy.deepcopy(self.original)
        edit(candidate)
        raw = (
            json.dumps(candidate, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        digest = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "projection.json").write_bytes(raw)
            pin = json.loads((SOURCE / "projection.sha256.json").read_text())
            pin["projection_sha256"] = digest
            (p / "projection.sha256.json").write_text(json.dumps(pin))
            with (
                patch.object(l2, "OUT", p),
                patch.object(l2, "PINNED_L2_SHA256", digest),
            ):
                with self.assertRaises(l2.L2APIError):
                    l2.load_l2()

    def test_bid_total_inconsistent(self):
        self.check_invalid_repin(
            lambda d: d["products"]["BTC-USD"]["depth_snapshots"][1][
                "depth"
            ].__setitem__("bid_total_base_10", "999")
        )

    def test_ask_total_inconsistent(self):
        self.check_invalid_repin(
            lambda d: d["products"]["ETH-USD"]["depth_snapshots"][1][
                "depth"
            ].__setitem__("ask_total_base_10", "999")
        )

    def test_negative_total(self):
        self.check_invalid_repin(
            lambda d: d["products"]["BTC-USD"]["depth_snapshots"][1][
                "depth"
            ].__setitem__("bid_total_base_10", "-0.1")
        )

    def test_bad_horizon(self):
        self.check_invalid_repin(lambda d: d.__setitem__("recovery_window_ms", 29_000))

    def test_bad_event_available_time(self):
        def edit(d):
            d["products"]["BTC-USD"]["recovery_events"][0]["available_at_ms"] -= 1000

        self.check_invalid_repin(edit)

    def test_bad_event_product(self):
        self.check_invalid_repin(
            lambda d: d["products"]["BTC-USD"]["recovery_events"][0].__setitem__(
                "product", "ETH-USD"
            )
        )

    def test_duplicate_event_timestamp(self):
        def edit(d):
            events = d["products"]["BTC-USD"]["recovery_events"]
            events[1]["shock_receiver_ms"] = events[0]["shock_receiver_ms"]
            events[1]["available_at_ms"] = events[0]["available_at_ms"]

        self.check_invalid_repin(edit)

    def test_bad_shock_depth(self):
        self.check_invalid_repin(
            lambda d: d["products"]["BTC-USD"]["recovery_events"][0].__setitem__(
                "shock_depth_base", "999"
            )
        )

    def test_bad_recovery_threshold(self):
        self.check_invalid_repin(
            lambda d: d["products"]["BTC-USD"]["recovery_events"][0].__setitem__(
                "recovery_threshold_base", "100"
            )
        )

    def test_recovered_without_delay(self):
        def edit(d):
            e = d["products"]["BTC-USD"]["recovery_events"][0]
            e["recovery_within_30s"] = True
            e["first_recovery_delay_ms"] = None

        self.check_invalid_repin(edit)

    def test_censored_with_delay(self):
        def edit(d):
            e = d["products"]["BTC-USD"]["recovery_events"][0]
            e["recovery_within_30s"] = False
            e["censored_at_30s"] = True
            e["first_recovery_delay_ms"] = 5000

        self.check_invalid_repin(edit)

    def test_wrong_censoring(self):
        def edit(d):
            e = d["products"]["BTC-USD"]["recovery_events"][0]
            e["censored_at_30s"] = e["recovery_within_30s"]

        self.check_invalid_repin(edit)

    def test_fractional_delay(self):
        self.check_invalid_repin(
            lambda d: d["products"]["BTC-USD"]["recovery_events"][0].__setitem__(
                "first_recovery_delay_ms", 1234
            )
        )


if __name__ == "__main__":
    unittest.main()
