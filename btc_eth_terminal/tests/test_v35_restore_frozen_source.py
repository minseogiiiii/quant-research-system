"""Safety and reproducibility gates for restoring the actual frozen v34.1 source."""
from __future__ import annotations

from pathlib import Path
import os
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import v35_restore_frozen_source as restore


class RestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.archive = Path(os.environ.get('FROZEN_V34_1_ZIP', ''))
        if not cls.archive.is_file():
            raise unittest.SkipTest('Requires explicit FROZEN_V34_1_ZIP at known SHA256')
        cls.source = restore.validated_archive(cls.archive)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'btc_eth_terminal'
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_original_source_is_verified(self):
        self.assertIn('backend/v34_l2_api.py', self.source)
        self.assertIn('backend/v34_l2_depth.py', self.source)
        self.assertTrue(all(restore.source_file(k) for k in self.source))

    def test_no_public_data_or_frontend_source_selected(self):
        self.assertFalse(any('/data/' in k or k.startswith('frontend/') for k in self.source))

    def test_dry_run_never_modifies_destination(self):
        r = restore.reconcile(self.archive, self.root)
        self.assertFalse(r['written'])
        self.assertFalse(any(self.root.rglob('*.py')))
        self.assertIn('backend/v35_live_api.py', r['v35_files_still_missing'])

    def test_restore_is_idempotent_and_preserves_existing_conflict(self):
        existing = self.root / 'backend' / 'bridge.py'
        existing.parent.mkdir(parents=True)
        existing.write_text('# local newer version\n')
        r = restore.reconcile(self.archive, self.root, apply=True)
        self.assertEqual(existing.read_text(), '# local newer version\n')
        self.assertIn('backend/bridge.py', r['conflicts_preserved'])
        self.assertEqual((self.root / 'backend/v34_l2_api.py').read_bytes(), self.source['backend/v34_l2_api.py'])
        r2 = restore.reconcile(self.archive, self.root, apply=False)
        self.assertIn('backend/v34_l2_api.py', r2['identical'])
        self.assertFalse(r2['written'])

    def test_symlink_destination_rejected(self):
        (self.root / 'backend').symlink_to(Path(self.temp.name), target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            restore.reconcile(self.archive, self.root)

    def test_wrong_archive_sha_fails_closed(self):
        bad = Path(self.temp.name) / 'fake.zip'
        bad.write_bytes(b'not a frozen artifact')
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            restore.validated_archive(bad)


if __name__ == '__main__':
    unittest.main()
