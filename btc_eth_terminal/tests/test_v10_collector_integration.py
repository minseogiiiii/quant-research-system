"""Collector-to-auditor contract with explicit fake source, never exchange evidence."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT / 'backend'))
from audit_soak import audit


@unittest.skipUnless(shutil.which('node'), 'Node.js required for capture collector')
class CollectorIntegration(unittest.TestCase):
    def run_mock(self, preset, expected_exit):
        with tempfile.TemporaryDirectory() as tmp:
            env=os.environ.copy()
            env['SOAK_OUTPUT_ROOT']=tmp
            env['SOAK_DURATION_SECONDS']='10'
            p=subprocess.run([shutil.which('node'), '--import',
                f'./tests/{preset}', 'scripts/btc_eth_soak_capture.mjs'],
                cwd=ROOT, env=env, capture_output=True,text=True,timeout=18)
            self.assertEqual(p.returncode,expected_exit,p.stdout[-1000:]+p.stderr[-1000:])
            info=json.loads(p.stdout.splitlines()[-1])
            folder=Path(info['directory'])
            self.assertEqual(folder.parent,Path(tmp))
            self.assertEqual(list(Path(tmp).iterdir()),[folder])
            return info,audit(folder)

    def test_real_collector_synthetic_success_with_atomic_chunk(self):
        info,check=self.run_mock('mock_coinbase_websocket.mjs',0)
        self.assertEqual(info['chunks'],1)
        self.assertEqual(info['connections'],1)
        self.assertTrue(check['eligible_for_point_in_time_feature_research'])
        self.assertEqual(check['connection_sequences'],{'0':[0,6]})
        self.assertFalse(check['executable_alpha_verified'])

    def test_real_collector_synthetic_gap_and_reconnect_quarantine(self):
        info,check=self.run_mock('mock_coinbase_gap.mjs',2)
        self.assertEqual(info['faults'],1)
        self.assertEqual(info['connections'],2)
        self.assertEqual(check['status'],'ARCHIVED_BUT_NOT_ADMITTED')
        self.assertEqual(check['connection_sequences'],{'0':[0,6],'1':[0,6]})
        self.assertFalse(check['eligible_for_point_in_time_feature_research'])
        self.assertFalse(check['orders_enabled'])

if __name__=='__main__':
    unittest.main()
