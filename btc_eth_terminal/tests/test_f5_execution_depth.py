import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from f4_depth_buckets import units
from f5_execution_depth import SCALE, build, project, walk
from test_f4_depth_buckets_archive import BASE
from test_f4_ofi_markout import packet
from test_f4_order_flow_archive import source

POLICY = dict(
    latency_ms=100,
    tolerance_ms=100,
    max_signal_age_ms=1000,
    capacity_fraction="1",
    fee_bps="10",
    extra_slippage_bps="0",
)


def intent(t=1000, qty="2", side="BUY"):
    return dict(id="x", product="BTC-USD", side=side, decision_ms=t, quantity=qty)


def book(rows):
    return {units(p): units(q) for p, q in rows}


class Depth(unittest.TestCase):
    def test_multi_level_both_sides(self):
        bid = book([("99", "1"), ("98", "3")])
        ask = book([("101", "1"), ("102", "3")])
        a = walk(bid, ask, "BUY", 2 * SCALE, SCALE, 10 * SCALE, 0)
        self.assertEqual(Decimal(a["vwap"]), Decimal("101.5"))
        self.assertEqual(Decimal(a["all_in_quote"]), Decimal("203.203"))
        b = walk(bid, ask, "SELL", 2 * SCALE, SCALE, 10 * SCALE, 0)
        self.assertEqual(Decimal(b["vwap"]), Decimal("98.5"))
        self.assertEqual(Decimal(b["all_in_quote"]), Decimal("196.803"))

    def test_capacity_and_insufficient_never_partial(self):
        b = book([("99", "1")])
        a = book([("101", "1"), ("102", "1")])
        r = walk(b, a, "BUY", 2 * SCALE, SCALE // 2, 0, 0)
        self.assertEqual(r["status"], "INSUFFICIENT_DISPLAYED_DEPTH")
        self.assertIsNone(r["vwap"])
        self.assertIsNone(r["all_in_quote"])
        self.assertEqual(Decimal(r["available_base_at_haircut"]), 1)

    def test_first_quote_and_no_later_retry(self):
        packets = [
            packet(0, 900, "snapshot", [("bid", "99", "3"), ("offer", "101", "1")]),
            packet(1, 1000, "update", [("offer", "101", "2")]),
            packet(2, 1100, "update", [("offer", "101", "0.5")]),
            packet(3, 1150, "update", [("offer", "101", "3")]),
        ]
        r = project(packets, [intent()], POLICY)["results"][0]
        self.assertEqual(r["signal_source_sequence"], 0)
        self.assertEqual(r["quote_source_sequence"], 2)
        self.assertEqual(r["status"], "INSUFFICIENT_DISPLAYED_DEPTH")
        self.assertIsNone(r["all_in_quote"])

    def test_no_same_time_or_stale(self):
        p = [
            packet(0, 1000, "snapshot", [("bid", "99", "3"), ("offer", "101", "3")]),
            packet(1, 1100, "update", [("offer", "101", "4")]),
        ]
        self.assertEqual(
            project(p, [intent()], POLICY)["results"][0]["status"],
            "NO_FRESH_SIGNAL_BOOK",
        )
        self.assertEqual(
            project(p, [intent(5000)], POLICY)["results"][0]["status"],
            "OUTSIDE_OBSERVED_SOURCE",
        )

    def test_sealed_fixture(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pin, _ = source(root)
            a = build(root, pin, [intent(BASE + 1000, "1")], POLICY)
            self.assertEqual(a["results"][0]["quote_receiver_ms"], BASE + 1100)
            self.assertEqual(a["results"][0]["status"], "INDICATIVE_FULL_DEPTH_QUOTE")
            self.assertFalse(a["actual_fills_verified"])
