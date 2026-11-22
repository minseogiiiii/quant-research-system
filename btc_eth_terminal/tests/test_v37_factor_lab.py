"""Q7 read-only research safety and archived receiver-time golden tests."""

import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from v23_causal_microstructure import features_from_events, ofi_delta
from v37_factor_lab import (
    DEST,
    PIN,
    PRODS,
    FactorLabReject,
    feature_rows_to_diagnostics,
    load_report,
    reference_mid,
    static_walk,
    view,
)

NOW = 1_790_000_000_000


def u(side, price, size):
    return {"side": side, "price_level": str(price), "new_quantity": str(size)}


def book():
    return {
        "bids": [{"price": "100", "quantity": "2"}, {"price": "99", "quantity": "3"}],
        "asks": [{"price": "101", "quantity": "1"}, {"price": "102", "quantity": "4"}],
    }


class FactorTests(unittest.TestCase):
    def test_ofi_golden_price_and_quantity(self):
        old = ((100, 2), (101, 1))
        self.assertEqual(ofi_delta(old, ((100, 3), (101, 1))), 1)
        self.assertEqual(ofi_delta(old, ((100, 2), (101, 2))), -1)
        self.assertEqual(ofi_delta(old, ((101, 1), (102, 1))), 2)
        self.assertEqual(ofi_delta(old, ((99, 10), (100, 1))), -3)
        self.assertEqual(ofi_delta(old, old), 0)

    def test_snapshot_not_flow_and_prefix_causal(self):
        start = NOW // 5000 * 5000
        events = [
            (
                start - 2000,
                0,
                "BTC-USD",
                "snapshot",
                [u("bid", 100, 2), u("offer", 101, 3)],
            ),
            (
                start - 2000,
                0,
                "ETH-USD",
                "snapshot",
                [u("bid", 20, 4), u("offer", 21, 2)],
            ),
            (start + 1000, 1, "BTC-USD", "update", [u("bid", 100, 4)]),
        ]
        one = features_from_events(iter(events), [start, start + 5000])
        self.assertEqual(one[0]["ofi_rolling_5m_base"], "0")
        self.assertEqual(one[2]["ofi_rolling_5m_base"], "2")
        more = events + [(start + 7000, 2, "BTC-USD", "update", [u("bid", 100, 50)])]
        two = features_from_events(iter(more), [start, start + 5000, start + 10000])
        self.assertEqual(one, two[: len(one)])

    def test_source_age_fail_closed(self):
        bad = {"status": "NO_ADMISSIBLE_QUOTE"}
        self.assertIsNone(reference_mid(bad))
        self.assertIsNone(
            reference_mid({"status": "DIAGNOSTIC_CAUSAL_L1", "source_age_ms": 5001})
        )

    def test_future_labels_never_in_features_and_censored(self):
        t = NOW // 60000 * 60000
        rows = []
        for p in PRODS:
            for d in range(0, 65_000, 5000):
                r = {
                    "instrument": p,
                    "decision_receive_utc_ms": t + d,
                    "status": "DIAGNOSTIC_CAUSAL_L1",
                    "source_age_ms": 0,
                    "best_bid": "100",
                    "best_ask": "101",
                    "best_bid_qty": "2",
                    "best_ask_qty": "1",
                    "microprice": "100.6",
                    "best_imbalance": "0.333",
                    "ofi_rolling_5m_base": "2.0",
                    "l2_update_events_rolling_5m": 2,
                    "feature_max_received_utc_ms": t + d,
                    "source_message_sequence": d // 5000,
                }
                rows.append(r)
        diag = feature_rows_to_diagnostics(rows)
        first = diag["BTC-USD"][0]
        self.assertNotIn("future", repr(first["features_asof"]))
        self.assertEqual(
            first["future_only_diagnostic_labels"]["60000"]["status"],
            "HISTORICAL_FUTURE_MIDPRICE_DIAGNOSTIC_ONLY",
        )
        last = diag["BTC-USD"][1]
        self.assertEqual(
            last["future_only_diagnostic_labels"]["60000"]["status"], "CENSORED"
        )

    def test_static_depth_book_walk_and_censor(self):
        d = book()
        x = static_walk(d, "BUY", "3", "10", "5")
        self.assertEqual(
            x["reference_vwap"], str((Decimal(101) + Decimal(102) * 2) / Decimal(3))
        )
        self.assertFalse(x["spread_double_counted"])
        self.assertFalse(x["real_fills"])
        self.assertEqual(x["fee_bps_assumed"], "10")
        self.assertEqual(
            x["total_indicative_taker_cost_bps_assumed"],
            str(
                (Decimal("101.6666666666666666666666667") - Decimal("100.5"))
                / Decimal("100.5")
                * 10000
                + 15
            ),
        )
        big = static_walk(d, "BUY", "7", "10", "5")
        self.assertEqual(big["status"], "CENSORED_INSUFFICIENT_DISPLAYED_DEPTH")
        self.assertIsNone(big["reference_vwap"])
        self.assertEqual(big["uncovered_base"], "2")
        self.assertEqual(static_walk(d, "SELL", "2", "0", "0")["reference_vwap"], "100")
        constrained = static_walk(d, "BUY", "3", "20", "10", "0.5")
        self.assertEqual(constrained["status"], "CENSORED_INSUFFICIENT_DISPLAYED_DEPTH")
        self.assertEqual(constrained["displayed_depth_fraction_assumed"], "0.5")
        for side in ("LONG", "SHORT"):
            with self.assertRaises(FactorLabReject):
                static_walk(d, side, "1", "1", "0")

    def test_negative_cost_assumptions_fail_closed(self):
        d = book()
        for fee, slip in (
            ("-10", "0"),
            ("0", "-100"),
            ("-10", "-200"),
            ("-0.000001", "0"),
            ("0", "-0.000001"),
        ):
            with self.subTest(fee=fee, slippage=slip):
                with self.assertRaises(FactorLabReject):
                    static_walk(d, "BUY", "1", fee, slip)
        for fraction in ("0", "-0.1", "1.00001"):
            with self.subTest(fraction=fraction):
                with self.assertRaises(FactorLabReject):
                    static_walk(d, "BUY", "1", "0", "0", fraction)
        for side in ("BUY", "SELL"):
            with self.subTest(side=side):
                x = static_walk(d, side, "1", "0", "0")
                self.assertEqual(x["fee_bps_assumed"], "0")
                self.assertEqual(x["extra_non_book_slippage_bps_assumed"], "0")
                self.assertGreaterEqual(
                    Decimal(x["total_indicative_taker_cost_bps_assumed"]), 0
                )

    def test_pinned_real_archived_report_and_no_asof_leak(self):
        obj, digest = load_report()
        self.assertEqual(obj["research_verdict"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(len(digest), 64)
        for p in PRODS:
            self.assertEqual(obj["sample_summary"][p]["feature_rows"], 60)
            self.assertEqual(
                obj["sample_summary"][p]["primary_60s_future_mid_labels"], 59
            )
            t = obj["source_start_receiver_ms"] + 120_000
            rv = view(p, t, "microstructure")
            self.assertTrue(all(r["decision_receiver_ms"] <= t for r in rv["rows"]))
            self.assertFalse(any("future" in k for r in rv["rows"] for k in r))
            research = view(p, t, "research")
            self.assertIsNone(research["predictive_effect"])
            self.assertEqual(research["trials_executed_on_blind"], 0)
            cost = view(p, t, "execution")
            self.assertTrue(
                all(x["decision_receiver_ms"] <= t for x in cost["samples"])
            )
        with self.assertRaises(FactorLabReject):
            view("SOL-USD", t, "research")
        with self.assertRaises(FactorLabReject):
            view("BTC-USD", t + 100000000, "research")

    def test_modified_report_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "report.json"
            q = Path(td) / "report.sha256.json"
            p.write_bytes(DEST.read_bytes() + b" ")
            q.write_bytes(PIN.read_bytes())
            with patch("v37_factor_lab.DEST", p), patch("v37_factor_lab.PIN", q):
                with self.assertRaises(FactorLabReject):
                    load_report()

    def test_pre_registration_is_not_blind_certificate(self):
        p = Path(__file__).resolve().parents[1] / "backend/q7_experiment_prereg.json"
        j = json.loads(p.read_text())
        self.assertEqual(j["primary_horizon_ms"], 60000)
        self.assertIsNone(j["split"]["blind_final"])
        self.assertEqual(j["trial_budget"], 8)
        self.assertEqual(
            j["development_archive_role"], "ALREADY_EXPLORED_FIXTURE_NOT_OOS"
        )
        self.assertFalse(j["orders_enabled"])


if __name__ == "__main__":
    unittest.main()
