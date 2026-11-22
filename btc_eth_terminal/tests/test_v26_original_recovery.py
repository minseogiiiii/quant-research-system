"""v26 byte recovery negative cases. All synthetic ZIP/CSV fixtures."""

from __future__ import annotations

import gzip
import stat
import sys
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from zipfile import ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def _q14_load_after_path_setup_0():
    from v26_original_pair_recovery import RecoveryError, recover

    return RecoveryError, recover


RecoveryError, recover = _q14_load_after_path_setup_0()
del _q14_load_after_path_setup_0


class RecoveryTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.archive = self.base / "backup.zip"
        self.dest = self.base / "out"
        self.good_features = gzip.compress(
            b"open_time,decision_at,available_at,feature1\n0,1,1,2\n"
        )
        self.good_labels = gzip.compress(
            b"open_time,decision_at,label_end,result\n0,1,4,1\n"
        )

    def tearDown(self):
        self.temp.cleanup()

    def pack(self, items):
        with ZipFile(self.archive, "w") as zf:
            for key, value in items.items():
                zf.writestr(key, value)

    def test_valid_pair_byte_identical_and_not_admitted(self):
        self.pack(
            {
                "s/btc_features_5m.csv.gz": self.good_features,
                "s/btc_future_labels.csv.gz": self.good_labels,
            }
        )
        out = recover([self.archive], self.dest)
        self.assertEqual(len(out["matching_originals"]), 2)
        self.assertEqual(
            out["filename_cohorts_with_both_originals_not_pit_verified"], ["btc"]
        )
        self.assertEqual(
            (self.dest / "btc_features_5m.csv.gz").read_bytes(), self.good_features
        )
        self.assertEqual(
            out["matching_originals"][0]["compressed_original_sha256"],
            sha256(self.good_features).hexdigest(),
        )
        self.assertFalse(out["research_admission_allowed"])

    def test_nested_zip_does_not_traverse(self):
        self.pack({"dir/../../btc_features_5m.csv.gz": self.good_features})
        with self.assertRaisesRegex(RecoveryError, "UNSAFE_ZIP_MEMBER"):
            recover([self.archive], self.dest)
        self.assertFalse((self.base / "btc_features_5m.csv.gz").exists())

    def test_absolute_zip_path_refused(self):
        self.pack({"/btc_features_5m.csv.gz": self.good_features})
        with self.assertRaisesRegex(RecoveryError, "UNSAFE_ZIP_MEMBER"):
            recover([self.archive], self.dest)

    def test_zip_symlink_entry_rejected(self):
        z = ZipInfo("btc_features_5m.csv.gz")
        z.create_system = 3
        z.external_attr = (stat.S_IFLNK | 0o777) << 16
        with ZipFile(self.archive, "w") as fd:
            fd.writestr(z, b"/etc/passwd")
        with self.assertRaisesRegex(RecoveryError, "UNSAFE_ZIP_MEMBER"):
            recover([self.archive], self.dest)

    def test_invalid_gzip_refused(self):
        self.pack({"btc_features_5m.csv.gz": b"notgzip"})
        with self.assertRaisesRegex(RecoveryError, "ZIP_OR_GZIP"):
            recover([self.archive], self.dest)

    def test_crc_corrupted_gzip_rejected(self):
        corrupted = self.good_features[:-3] + b"XYZ"
        self.pack({"btc_features_5m.csv.gz": corrupted})
        with self.assertRaisesRegex(RecoveryError, "ZIP_OR_GZIP"):
            recover([self.archive], self.dest)

    def test_empty_header_rejected(self):
        self.pack({"btc_features_5m.csv.gz": gzip.compress(b",x\n0,1\n")})
        with self.assertRaisesRegex(RecoveryError, "CSV_HEADER_EMPTY"):
            recover([self.archive], self.dest)

    def test_duplicate_csv_columns_rejected(self):
        self.pack({"btc_features_5m.csv.gz": gzip.compress(b"a,a\n1,2\n")})
        with self.assertRaisesRegex(RecoveryError, "CSV_DUPLICATE"):
            recover([self.archive], self.dest)

    def test_new_directory_required(self):
        self.pack({"btc_features_5m.csv.gz": self.good_features})
        self.dest.mkdir()
        with self.assertRaisesRegex(RecoveryError, "DESTINATION_MUST_BE_NEW"):
            recover([self.archive], self.dest)

    def test_existing_symlink_dest_rejected(self):
        self.pack({"btc_features_5m.csv.gz": self.good_features})
        self.dest.symlink_to(self.base, target_is_directory=True)
        with self.assertRaisesRegex(RecoveryError, "DESTINATION_MUST_BE_NEW"):
            recover([self.archive], self.dest)

    def test_cross_zip_collision_fails_no_overwrite(self):
        other = self.base / "other.zip"
        self.pack({"btc_features_5m.csv.gz": self.good_features})
        with ZipFile(other, "w") as zf:
            zf.writestr("btc_features_5m.csv.gz", gzip.compress(b"a\n1\n"))
        with self.assertRaisesRegex(RecoveryError, "CONFLICTING_OR_DUPLICATE"):
            recover([self.archive, other], self.dest)
        self.assertEqual(
            (self.dest / "btc_features_5m.csv.gz").read_bytes(), self.good_features
        )

    def test_no_valid_file_not_admitted(self):
        self.pack({"readme.md": b"hi"})
        out = recover([self.archive], self.dest)
        self.assertEqual(out["status"], "NO_MATCHING_ORIGINAL_BYTES")
        self.assertFalse(out["point_in_time_verified"])

    def test_unsafe_backslash_rejected(self):
        self.pack({"a\\btc_features_5m.csv.gz": self.good_features})
        with self.assertRaisesRegex(RecoveryError, "UNSAFE_ZIP_MEMBER"):
            recover([self.archive], self.dest)

    def test_missing_zip_refused(self):
        with self.assertRaisesRegex(RecoveryError, "INVALID_ARCHIVE_INPUT"):
            recover([self.base / "not_found.zip"], self.dest)

    def test_symlink_archive_refused(self):
        self.pack({"btc_features_5m.csv.gz": self.good_features})
        link = self.base / "link.zip"
        link.symlink_to(self.archive)
        with self.assertRaisesRegex(RecoveryError, "INVALID_ARCHIVE_INPUT"):
            recover([link], self.dest)
