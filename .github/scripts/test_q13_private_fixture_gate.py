"""Pure, fixture-free tests for the Q13 private historical archive guard."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("q13_private_fixture_gate.py")
SPEC = importlib.util.spec_from_file_location("q13_private_fixture_gate", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Q13GuardUnitTests(unittest.TestCase):
    def test_nonexistent_private_archive_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(
            ValueError, "RELEASE_MISSING"
        ):
            MODULE.verify_zip(Path(tmp) / "no_private_archive.zip")

    def test_tiny_substitute_archive_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake.zip"
            fake.write_bytes(b"fake market data")
            with self.assertRaisesRegex(ValueError, "RELEASE_MISSING_OR_SIZE_MISMATCH"):
                MODULE.verify_zip(fake)

    def test_checkout_source_bytes_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            checkout = Path(tmp) / "checkout"
            for part in ("src", "tests"):
                source = root / "frontend" / part / "same.txt"
                stage = checkout / "btc_eth_terminal/frontend" / part / "same.txt"
                source.parent.mkdir(parents=True)
                stage.parent.mkdir(parents=True)
                source.write_text("trusted old bytes")
                stage.write_text("trusted old bytes")
            MODULE.compare_checkout(root, checkout)
            changed = checkout / "btc_eth_terminal/frontend/src/same.txt"
            changed.write_text("different bytes")
            with self.assertRaisesRegex(ValueError, "STAGED_SOURCE_MISMATCH"):
                MODULE.compare_checkout(root, checkout)

    def test_checkout_file_added_not_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            checkout = Path(tmp) / "checkout"
            for part in ("src", "tests"):
                (root / "frontend" / part).mkdir(parents=True)
                (checkout / "btc_eth_terminal/frontend" / part).mkdir(parents=True)
            (checkout / "btc_eth_terminal/frontend/src/injected.ts").write_text("x")
            with self.assertRaisesRegex(ValueError, "FILESET_MISMATCH"):
                MODULE.compare_checkout(root, checkout)


if __name__ == "__main__":
    unittest.main()
