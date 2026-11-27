import copy
import tempfile
import unittest
from pathlib import Path
from f5_predictive_diagnostics import summarize, metric, ranks, build_diagnostics
from test_f4_order_flow_archive import source
from test_f4_depth_buckets_archive import BASE
from decimal import Decimal


def fixture(n=30):
    p = dict(
        start_ms=10000,
        end_ms=10000 + n * 1000,
        step_ms=1000,
        window_ms=500,
        max_age_ms=100,
        tolerance_ms=50,
        horizons_ms=[100],
    )
    features = []
    labels = []
    for i, t in enumerate(range(p["start_ms"], p["end_ms"], 1000)):
        features.append(
            dict(
                decision_ms=t,
                status="CAUSAL_FEATURE",
                feature_available_ms=t - 1,
                mid="100",
                microprice="100",
                imbalance="0",
                ofi_base=str(i - 15),
            )
        )
        labels.append(
            dict(
                decision_ms=t,
                horizon_ms=100,
                target_ms=t + 100,
                status="OBSERVED_FUTURE_LABEL",
                label_available_ms=t + 100,
                midquote_return_bps=str(i - 15),
            )
        )
    return dict(
        schema="f4_causal_ofi_markout_v1",
        mode="ARCHIVE_REPLAY",
        product="BTC-USD",
        source_manifest_sha256="a" * 64,
        epoch="a" * 64 + ":0",
        policy=p,
        features=features,
        future_labels=labels,
        source_last_receiver_ms=p["end_ms"],
        live_feed=False,
        execution_authorized=False,
        zero_gap_certified=False,
        oos_approved=False,
        alpha_confirmed=False,
    )


class Diagnostics(unittest.TestCase):
    def test_correlation_length_mismatch_is_rejected(self):
        from f5_predictive_diagnostics import corr

        with self.assertRaisesRegex(ValueError, "CORRELATION_LENGTH_MISMATCH"):
            corr(list(range(30)), list(range(29)))

    def test_correlation_short_mismatch_is_rejected(self):
        from f5_predictive_diagnostics import corr

        with self.assertRaisesRegex(ValueError, "CORRELATION_LENGTH_MISMATCH"):
            corr([1], [])

    def test_perfect_and_constant(self):
        r = summarize(fixture())["results"][0]
        self.assertEqual(r["disjoint_pairs"], 30)
        self.assertAlmostEqual(r["features"][0]["all_pairs"]["pearson_ic"], 1)
        self.assertAlmostEqual(r["features"][0]["all_pairs"]["rank_ic"], 1)
        self.assertIsNone(r["features"][1]["all_pairs"]["rank_ic"])
        self.assertEqual(ranks([3, 1, 1]), [3, 1.5, 1.5])

    def test_small_and_overlapping(self):
        r = fixture()
        r["policy"]["window_ms"] = 5000
        a = summarize(r)["results"][0]
        self.assertEqual(a["disjoint_pairs"], 5)
        self.assertIsNone(a["features"][0]["disjoint_intervals"]["pearson_ic"])
        self.assertIsNone(
            summarize(fixture(5))["results"][0]["features"][0]["all_pairs"]["rank_ic"]
        )

    def test_reject_future_and_duplicate(self):
        for mutate in (
            lambda r: r["features"][0].update(feature_available_ms=10000),
            lambda r: r["future_labels"][0].update(label_available_ms=10099),
            lambda r: r["future_labels"].append(r["future_labels"][0]),
            lambda r: r["future_labels"].pop(),
            lambda r: r.update(oos_approved=True),
        ):
            r = fixture()
            mutate(r)
            with self.assertRaises(ValueError):
                summarize(r)

    def test_missing_never_zero_fill(self):
        r = fixture()
        r["future_labels"][0].update(
            status="NO_FUTURE_OBSERVATION",
            label_available_ms=None,
            midquote_return_bps=None,
        )
        a = summarize(r)["results"][0]
        self.assertEqual(a["paired_labels"], 29)
        self.assertEqual(a["missing_labels"], 1)

    def test_real_format_fixture(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pin, _ = source(root)
            r = build_diagnostics(
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
            self.assertTrue(
                r["independent_source_witness"]["independent_software_parity"]
            )
            self.assertFalse(r["oos_approved"])
