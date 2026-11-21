"""Inventory is forensic-only: filename discovery never upgrades to PIT."""
import gzip
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from zipfile import ZipFile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from v25_original_pair_inventory import inventory

class InventoryTests(TestCase):
    def test_empty_root_is_explicitly_blocked(self):
        with TemporaryDirectory() as d:
            x=inventory([d]);self.assertEqual(x['status'],'BLOCKED_ORIGINAL_BYTES_NOT_LOCATED')
    def test_matching_names_not_evidence(self):
        with TemporaryDirectory() as d:
            root=Path(d)
            for kind in ['features_5m','future_labels']:
                (root/('btc_'+kind+'.csv.gz')).write_bytes(gzip.compress(b'test\n'))
            x=inventory([root]);self.assertEqual(len(x['candidates']),2)
            self.assertEqual(x['cohort_prefixes_with_both_filenames_only'],['btc'])
            self.assertFalse(x['research_oos_admission_allowed'])
            self.assertFalse(x['historical_availability_verified'])
            self.assertTrue(all(item['sha256_compressed_bytes'] for item in x['candidates']))
    def test_zip_discovery_not_byte_authentication(self):
        with TemporaryDirectory() as d:
            root=Path(d)
            with ZipFile(root/'backup.zip','w') as z:
                z.writestr('old/btc_features_5m.csv.gz',b'xyz')
            x=inventory([root]);self.assertEqual(len(x['candidates']),1)
            self.assertIsNone(x['candidates'][0]['sha256_compressed_bytes'])
    def test_symlink_file_not_followed(self):
        with TemporaryDirectory() as d:
            root=Path(d)
            (root/'secret').write_bytes(b'secret')
            (root/'btc_features_5m.csv.gz').symlink_to(root/'secret')
            x=inventory([root]);self.assertEqual(x['candidates'],[])
    def test_missing_root_reports_failure(self):
        x=inventory([Path('/definitely-not-here-btc-eth-test')]);self.assertTrue(x['errors_or_limits'])
    def test_wrong_extension_not_candidate(self):
        with TemporaryDirectory() as d:
            (Path(d)/'btc_features_5m.csv').write_text('a')
            self.assertFalse(inventory([d])['candidates'])
