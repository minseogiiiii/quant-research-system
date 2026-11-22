"""Unit tests for Q12 Ruff baseline and staged-delta comparison."""

import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("q12_ruff_compare.py")
SPEC = spec_from_file_location("q12_ruff_compare", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def issue(row: int, source: str = "import os", code: str = "F401") -> dict:
    return {
        "path": "btc_eth_terminal/tests/example.py",
        "code": code,
        "message": "unused import",
        "source_line": source,
        "row": row,
    }


def report(before: list[dict], after: list[dict]) -> dict:
    return MODULE.compare(
        before,
        after,
        baseline_sha="a" * 40,
        head_sha="b" * 40,
        base_paths={"btc_eth_terminal/tests/example.py"},
    )


class RuffDeltaTests(unittest.TestCase):
    def test_shifted_lines_are_not_new_issues(self) -> None:
        out = report([issue(5)], [issue(550)])
        self.assertEqual(out["introduced_violations"], 0)
        self.assertTrue(out["delta_clean"])

    def test_duplicate_violations_preserve_multiplicity(self) -> None:
        out = report([issue(5)], [issue(5), issue(7)])
        self.assertEqual(out["introduced_violations"], 1)
        self.assertEqual(out["introduced_by_rule"], {"F401": 1})

    def test_new_source_line_is_visible(self) -> None:
        out = report([issue(5)], [issue(5, "import sys")])
        self.assertEqual(out["introduced_violations"], 1)
        self.assertEqual(out["resolved_violations"], 1)
        self.assertFalse(out["full_validation_passed"])

    def test_new_file_is_classified_as_new(self) -> None:
        result = MODULE.compare(
            [],
            [{**issue(2), "path": "btc_eth_terminal/new_test.py"}],
            baseline_sha="a" * 40,
            head_sha="b" * 40,
            base_paths=set(),
        )
        self.assertEqual(result["introduced_on_new_files"], 1)
        self.assertEqual(result["introduced_on_existing_files"], 0)
        self.assertFalse(result["production_acceptance"])


if __name__ == "__main__":
    unittest.main()
