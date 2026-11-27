import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from f4_ofi_markout import build, project
from test_f4_depth_buckets_archive import BASE
from test_f4_order_flow_archive import source


def packet(i, t, kind, levels, product="BTC-USD"):
    return (
        i,
        t,
        {
            "channel": "l2_data",
            "events": [
                {
                    "type": kind,
                    "product_id": product,
                    "updates": [
                        dict(side=s, price_level=p, new_quantity=q)
                        for s, p, q in levels
                    ],
                }
            ],
        },
    )


def sample():
    return [
        packet(0, 1000, "snapshot", [("bid", "100", "2"), ("offer", "102", "3")]),
        packet(1, 1500, "update", [("bid", "100", "4")]),
        packet(2, 2000, "update", [("offer", "102", "1")]),
        packet(3, 2500, "update", [("offer", "102", "0"), ("offer", "104", "2")]),
        packet(4, 3000, "update", [("bid", "100", "5")], product="ETH-USD"),
        packet(5, 3200, "update", [("bid", "100", "5")]),
    ]


def run(packets, **kwargs):
    policy = dict(
        product="BTC-USD",
        start_ms=2000,
        end_ms=4000,
        step_ms=1000,
        window_ms=1000,
        max_age_ms=1000,
        horizons_ms=[500, 1000],
        tolerance_ms=250,
    )
    policy.update(kwargs)
    return project(packets, **policy)


class OFI(unittest.TestCase):
    def test_strict_boundary_and_first_future_packet(self):
        r = run(sample())
        f = r["features"]
        self.assertEqual(f[0]["source_sequence"], 1)
        self.assertEqual(Decimal(f[0]["ofi_base"]), 2)
        self.assertEqual(Decimal(f[0]["mid"]), 101)
        self.assertEqual(Decimal(f[1]["ofi_base"]), 3)
        self.assertEqual(r["future_labels"][0]["label_source_sequence"], 3)
        self.assertEqual(r["future_labels"][0]["label_available_ms"], 2500)
        self.assertAlmostEqual(
            float(r["future_labels"][0]["midquote_return_bps"]), 10000 / 101
        )
        self.assertEqual(r["future_labels"][1]["label_source_sequence"], 5)
        self.assertEqual(r["future_labels"][2]["status"], "NO_FUTURE_OBSERVATION")

    def test_future_mutation_cannot_change_past_features(self):
        a = run(sample())
        b = sample()
        b[3] = packet(3, 2500, "update", [("offer", "102", "0"), ("offer", "106", "2")])
        c = run(b)
        self.assertEqual(a["features"][0], c["features"][0])
        self.assertNotEqual(a["future_labels"][0], c["future_labels"][0])

    def test_missing_warmup_staleness_and_capture_tail(self):
        a = run(sample(), start_ms=1000, end_ms=5000, max_age_ms=100)
        self.assertEqual(
            [x["status"] for x in a["features"]],
            [
                "INSUFFICIENT_OBSERVED_WARMUP",
                "NO_FRESH_QUOTE",
                "NO_FRESH_QUOTE",
                "OUTSIDE_OBSERVED_SOURCE",
            ],
        )
        self.assertFalse(a["future_labels"])

    def test_missed_horizon_not_filled_with_later_quote(self):
        r = run(sample(), tolerance_ms=0)
        self.assertEqual(r["future_labels"][1]["status"], "FIRST_L2_OUTSIDE_TOLERANCE")
        self.assertIsNone(r["future_labels"][1]["midquote_return_bps"])

    def test_reject_grid_sequence_and_cross(self):
        for args in (
            dict(horizons_ms=[500, 500]),
            dict(window_ms=0),
            dict(step_ms=101),
        ):
            with self.assertRaises(ValueError):
                run(sample(), **args)
        with self.assertRaises(ValueError):
            run(sample()[1:])
        b = sample()
        b[2] = packet(2, 2000, "update", [("bid", "103", "1")])
        with self.assertRaisesRegex(ValueError, "CROSSED"):
            run(b)

    def test_real_archive_format_synthetic_source(self):
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
            self.assertEqual(r["source_manifest_sha256"], pin)
            self.assertEqual(r["features"][0]["status"], "CAUSAL_FEATURE")
            self.assertEqual(r["future_labels"][0]["label_available_ms"], BASE + 1100)
            self.assertFalse(r["oos_approved"])
