"""Live-branch source exact-byte integration using fake WebSocket, never venue data."""
from __future__ import annotations
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from audit_soak import audit


@unittest.skipUnless(shutil.which('node'), 'Node >=22 required')
class LiveProbeIntegration(unittest.TestCase):
    def run_probe(self, preset, expected_code):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = ['node', '--import', str(ROOT / 'tests' / preset),
                   str(ROOT / 'scripts' / 'btc_eth_live_chunked_probe.mjs')]
            env = __import__('os').environ.copy()
            env['SOAK_DURATION_SECONDS'] = '15'
            p = subprocess.run(cmd, cwd=tmp, env=env, capture_output=True,
                               text=True, timeout=24)
            self.assertEqual(p.returncode, expected_code, p.stdout[-800:] + p.stderr[-800:])
            info = json.loads(p.stdout.splitlines()[-1])
            root = Path(tmp) / info['directory']
            self.assertTrue(root.is_dir())
            self.assertEqual(list(root.parent.iterdir()), [root])
            return info, audit(root)

    def test_live_branch_collector_fake_source_replay(self):
        info, result = self.run_probe('mock_coinbase_periodic.mjs', 0)
        self.assertGreaterEqual(info['messages'], 10)
        self.assertEqual(result['connection_sequences']['0'][0], 0)
        self.assertTrue(result['eligible_for_point_in_time_feature_research'])
        self.assertFalse(result['executable_alpha_verified'])

    def test_live_branch_collector_fake_gap_quarantine(self):
        info, result = self.run_probe('mock_coinbase_gap_periodic.mjs', 2)
        self.assertEqual(info['connections'], 2)
        self.assertEqual(info['faults'], 1)
        self.assertEqual(result['status'], 'ARCHIVED_BUT_NOT_ADMITTED')
        self.assertFalse(result['eligible_for_point_in_time_feature_research'])

if __name__ == '__main__':
    unittest.main()
