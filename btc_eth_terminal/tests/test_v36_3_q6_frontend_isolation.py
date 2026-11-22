"""Q6 source-pinned v36 bundle must never overwrite frozen legacy /research."""

from __future__ import annotations

import ast
import unittest
from hashlib import sha256
from pathlib import Path

from v34_frontend_integrity import verify_build as verify_legacy
from v36_frontend_integrity import (
    DIST,
    LIVE_JS,
    MANIFEST,
    PINNED_MANIFEST_SHA256,
    v36_asset,
)


class Q6FrontendIsolation(unittest.TestCase):
    def test_q6_is_a_distinct_pinned_bundle_with_liveness_guard(self):
        original, _ = verify_legacy()
        index, _ = v36_asset("/research/v36")
        js, kind = v36_asset(LIVE_JS)
        self.assertIn(b"index-BpiBDnMe.js", index)
        self.assertIn(b"HEALTH_PROBE_STALE_OR_CLOCK_ANOMALY", js)
        self.assertEqual(kind, "application/javascript")
        self.assertEqual(
            sha256(MANIFEST.read_bytes()).hexdigest(), PINNED_MANIFEST_SHA256
        )
        self.assertIn("index.html", original)
        self.assertNotEqual(original["index.html"], index)
        self.assertEqual(DIST.name, "dist_v36")

    def test_versioned_build_script_never_targets_legacy_dist(self):
        """Inspect Vite and promotion AST, not quote style or code layout."""
        script = (
            Path(__file__).resolve().parents[1] / "scripts/v36_build_frontend.py"
        ).read_text()
        tree = ast.parse(script)
        build = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "build"
        )
        main = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        calls = [node for node in ast.walk(build) if isinstance(node, ast.Call)]
        historical_checks = [
            node
            for node in calls
            if isinstance(node.func, ast.Name)
            and node.func.id == "verify_historical"
        ]
        self.assertEqual(len(historical_checks), 1)

        output_calls = [
            node
            for node in calls
            if isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and node.func.attr == "run"
            and node.args
            and isinstance(node.args[0], ast.List)
            and any(
                isinstance(arg, ast.Constant) and arg.value == "--outDir"
                for arg in node.args[0].elts
            )
        ]
        self.assertEqual(len(output_calls), 1)
        vite_call = output_calls[0]
        self.assertLess(historical_checks[0].lineno, vite_call.lineno)
        tokens = vite_call.args[0].elts
        self.assertTrue(
            any(isinstance(x, ast.Constant) and x.value == "build" for x in tokens)
        )
        index = next(
            index
            for index, token in enumerate(tokens)
            if isinstance(token, ast.Constant) and token.value == "--outDir"
        )
        self.assertLess(index + 1, len(tokens))
        destination = tokens[index + 1]
        self.assertIsInstance(destination, ast.Attribute)
        self.assertEqual(destination.attr, "name")
        self.assertIsInstance(destination.value, ast.Name)
        self.assertEqual(destination.value.id, "staging")

        promote = [
            node
            for node in ast.walk(main)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "--promote"
        ]
        self.assertEqual(len(promote), 1)
        self.assertTrue(
            any(
                keyword.arg == "action"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == "store_true"
                for keyword in promote[0].keywords
            )
        )
        for node in calls:
            if not (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "shutil"
            ):
                continue
            if node.func.attr == "rmtree" and node.args:
                self.assertNotEqual(ast.unparse(node.args[0]), "FE / 'dist'")
            if node.func.attr == "copytree" and len(node.args) >= 2:
                self.assertNotEqual(
                    (ast.unparse(node.args[0]), ast.unparse(node.args[1])),
                    ("FE / 'dist'", "FE / 'dist_v36'"),
                )


if __name__ == "__main__":
    unittest.main()
