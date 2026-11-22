#!/usr/bin/env python3
"""Small archive-independent safety regression for the exact staged Q7 functions.

This is a partial source-contract check; not the full Q7 runtime or cost/OOS proof.
The complete Q7 suite must independently import the released module with fixtures.
"""

from __future__ import annotations

import ast
import unittest
from decimal import Decimal, InvalidOperation
from pathlib import Path


HERE = Path(__file__).resolve().parents[2]
SOURCE = HERE / "btc_eth_terminal/backend/v37_factor_lab.py"


class FactorLabReject(ValueError):
    pass


def exact_cost_functions(text: str):
    """Compile the real function bodies without absent historical imports/data."""
    module = ast.parse(text, filename=str(SOURCE))
    required = ("dec", "static_walk")
    selected = [node for node in module.body
                if isinstance(node, ast.FunctionDef) and node.name in required]
    if tuple(node.name for node in selected) != required:
        raise ValueError("MISSING_OR_REORDERED_PINNED_COST_FUNCTIONS")
    isolated = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    env = {"Decimal": Decimal, "InvalidOperation": InvalidOperation,
           "FactorLabReject": FactorLabReject}
    exec(compile(isolated, str(SOURCE), "exec"), env)  # reviewed, pinned local source
    return env["static_walk"]


def book():
    return {
        "bids": [{"price": "100", "quantity": "2"},
                 {"price": "99", "quantity": "3"}],
        "asks": [{"price": "101", "quantity": "1"},
                 {"price": "102", "quantity": "4"}],
    }


class StaticCostSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE.read_text(encoding="utf-8")
        cls.walk = staticmethod(exact_cost_functions(cls.source))

    def test_negative_fee_and_slippage_always_rejected(self):
        for fee, slip in (("-10", "0"), ("0", "-100"),
                          ("-0.0000001", "0"), ("0", "-0.0000001"),
                          ("-10", "-200")):
            with self.subTest(fee=fee, slip=slip), self.assertRaises(FactorLabReject):
                self.walk(book(), "BUY", "1", fee, slip)

    def test_valid_zero_costs_and_displayed_book_walk(self):
        x = self.walk(book(), "BUY", "1", "0", "0")
        self.assertEqual(x["reference_vwap"], "101")
        self.assertEqual(x["fee_bps_assumed"], "0")
        self.assertEqual(x["extra_non_book_slippage_bps_assumed"], "0")
        self.assertFalse(x["real_fills"])
        self.assertFalse(x["spread_double_counted"])
        self.assertGreater(Decimal(x["total_indicative_taker_cost_bps_assumed"]), 0)

    def test_reject_invalid_depth_fraction_and_nonfinite(self):
        for fraction in ("0", "-0.1", "1.00001", "NaN", "Infinity"):
            with self.subTest(fraction=fraction), self.assertRaises(FactorLabReject):
                self.walk(book(), "BUY", "1", "0", "0", fraction)

    def test_censored_depth_does_not_invent_fill_or_cost(self):
        x = self.walk(book(), "BUY", "99", "10", "10")
        self.assertEqual(x["status"], "CENSORED_INSUFFICIENT_DISPLAYED_DEPTH")
        self.assertIsNone(x["reference_vwap"])
        self.assertIsNone(x["total_indicative_taker_cost_bps_assumed"])

    def test_negative_fee_mutation_is_detected(self):
        new = "(not positive and d < 0)"
        old = "(not positive and not allow_zero and d < 0)"
        self.assertIn(new, self.source)
        altered = exact_cost_functions(self.source.replace(new, old, 1))
        result = altered(book(), "BUY", "1", "-100", "0")
        self.assertLess(Decimal(result["total_indicative_taker_cost_bps_assumed"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
