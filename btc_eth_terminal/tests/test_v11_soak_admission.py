"""Regression for v11 evidence-admission hardening, using synthetic fixtures."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT / 'tests'))
from audit_soak import audit, SoakAuditError
from test_v10_soak import create_archive, stamp


class V11AdmissionHardening(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'source'
        create_archive(self.root)

    def modify(self, fn):
        p=self.root/'manifest.json'
        m=json.loads(p.read_text())
        fn(m)
        p.write_text(json.dumps(m))

    def test_valid_fixture_passes_new_boundaries(self):
        self.assertEqual(audit(self.root)['status'], 'INTERNALLY_REPLAYED_NO_KNOWN_GAP')

    def test_short_declared_capture_rejected_even_when_archives_valid(self):
        def change(m):
            start=datetime.fromisoformat(m['sampled_from_utc'].replace('Z','+00:00'))
            short=stamp(start+timedelta(seconds=3))
            m['finished_at_utc']=short
            m['connections'][0]['ended_utc']=short
        self.modify(change)
        with self.assertRaisesRegex(SoakAuditError, 'declared duration'):
            audit(self.root)

    def test_extra_rejected_source_file_blocks_clean_admission(self):
        (self.root/'rejected_0_1234.txt').write_text('rejected packet')
        with self.assertRaisesRegex(SoakAuditError, 'unlisted root'):
            audit(self.root)

    def test_pending_write_blocks_clean_admission(self):
        (self.root/'manifest.json.tmp').write_text('pending')
        with self.assertRaises(SoakAuditError):
            audit(self.root)

    def test_backward_connection_window_is_invalid(self):
        def change(m):
            start=datetime.fromisoformat(m['connections'][0]['started_utc'].replace('Z','+00:00'))
            m['connections'][0]['ended_utc']=stamp(start-timedelta(milliseconds=500))
        self.modify(change)
        with self.assertRaisesRegex(SoakAuditError, 'connection outside'):
            audit(self.root)

if __name__=='__main__':
    unittest.main()
