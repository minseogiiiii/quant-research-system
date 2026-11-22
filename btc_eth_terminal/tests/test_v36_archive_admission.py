"""v36 archive browser path uses ORIGINAL v35 ingress and rehashes files.
No archived bytes are modified; adversarial copies live in temporary dirs.
"""

import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.v36_play_verified_archive import admitted_verified_chunks
from v35_archive_ingress import ArchiveRejected

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy"
)


class ImmutableArchiveForBrowser(unittest.TestCase):
    def setUp(self):
        if not SOURCE.is_dir():
            self.skipTest("Original source not available")
        self.pin = hashlib.sha256((SOURCE / "manifest.json").read_bytes()).hexdigest()

    def test_real_clean_source_genuine_ingress_full_count(self):
        it = admitted_verified_chunks(SOURCE, self.pin)
        cert, _ = next(it)
        self.assertEqual(cert["admission"], "VERIFIED_ARCHIVE_ENVELOPE")
        self.assertEqual(cert["manifest_sha256"], self.pin)
        self.assertEqual(sum(chunk.count(b"\n") for _, chunk in it), 5177)

    def test_wrong_externally_pinned_manifest_denied(self):
        with self.assertRaises(ArchiveRejected):
            list(admitted_verified_chunks(SOURCE, "f" * 64))

    def test_altered_copy_denied_without_touching_original(self):
        with tempfile.TemporaryDirectory(prefix="v36_mutated_archive_") as tmp:
            dst = Path(tmp) / "source"
            shutil.copytree(SOURCE, dst)
            ch = next((dst / "chunks").glob("chunk_*.jsonl"))
            data = ch.read_bytes()
            ch.write_bytes(data[:-10] + b"0123456789")
            with self.assertRaises(ArchiveRejected):
                list(admitted_verified_chunks(dst, self.pin))

    def test_symlink_substituted_chunk_denied(self):
        with tempfile.TemporaryDirectory(prefix="v36_symlink_archive_") as tmp:
            dst = Path(tmp) / "source"
            shutil.copytree(SOURCE, dst)
            ch = next((dst / "chunks").glob("chunk_*.jsonl"))
            alternative = Path(tmp) / "copy"
            alternative.write_bytes(ch.read_bytes())
            ch.unlink()
            os.symlink(alternative, ch)
            with self.assertRaises((ArchiveRejected, OSError)):
                list(admitted_verified_chunks(dst, self.pin))


if __name__ == "__main__":
    unittest.main()
