"""Mutation tests proving Q6 source validation inspects actual AST behavior."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
TEST = ROOT / "btc_eth_terminal/tests/test_v36_3_q6_frontend_isolation.py"
SCRIPT = ROOT / "btc_eth_terminal/scripts/v36_build_frontend.py"
SPEC = importlib.util.spec_from_file_location("q13_q6_guard", TEST)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Q6ViteAstMutationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_six_critical_unsafe_modifications_are_detected(self) -> None:
        original = self.source
        cases = {
            "missing_outdir": (
                '"--outDir"', '"--notOutDir"',
            ),
            "output_to_legacy": (
                '"--outDir", staging.name',
                '"--outDir", (FE / "dist").name',
            ),
            "no_historical_integrity": (
                "    verify_historical()", "    pass  # mutant",
            ),
            "no_promotion_switch": (
                '"--promote",', '"--unsafe-promotion",',
            ),
            "delete_legacy": (
                '    staging = FE / ".dist_v36_candidate_unpromoted"',
                '    shutil.rmtree(FE / "dist")\n'
                '    staging = FE / ".dist_v36_candidate_unpromoted"',
            ),
            "copy_legacy": (
                '    staging = FE / ".dist_v36_candidate_unpromoted"',
                '    shutil.copytree(FE / "dist", FE / "dist_v36")\n'
                '    staging = FE / ".dist_v36_candidate_unpromoted"',
            ),
        }
        guard = MODULE.Q6FrontendIsolation()
        guard.test_versioned_build_script_never_targets_legacy_dist()
        for name, (before, after) in cases.items():
            self.assertIn(before, original, name)
            mutated = original.replace(before, after, 1)
            self.assertNotEqual(mutated, original, name)
            with self.subTest(mutation=name), patch.object(
                Path, "read_text", return_value=mutated,
            ), self.assertRaises(AssertionError):
                guard.test_versioned_build_script_never_targets_legacy_dist()


if __name__ == "__main__":
    unittest.main()
