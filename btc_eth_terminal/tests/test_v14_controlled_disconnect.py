"""Safety contract for explicit market-data discontinuity and reconnect.

The provider simulation does not prove a real outage or exchange sequencing.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from audit_soak import audit, SoakAuditError
from independent_soak_replay import replay
from test_v10_soak import create_archive


class FaultAdmissionPolicy(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / 'source'
        create_archive(self.source)

    def mutate(self, transform):
        path = self.source / 'manifest.json'
        manifest = json.loads(path.read_text())
        transform(manifest)
        path.write_text(json.dumps(manifest))

    def test_valid_normal_without_injection_remains_accepted(self):
        self.assertEqual(audit(self.source)['status'], 'INTERNALLY_REPLAYED_NO_KNOWN_GAP')

    def test_fault_injection_cannot_claim_clean_continuous_source(self):
        self.mutate(lambda m: m.__setitem__('controlled_fault_injection', {
            'mode': 'PUBLIC_FEED_DISCONNECT', 'after_messages': 7, 'never_admit': True}))
        with self.assertRaisesRegex(SoakAuditError, 'cannot be promoted'):
            audit(self.source)

    def test_bad_fault_type_rejected(self):
        self.mutate(lambda m: m.__setitem__('controlled_fault_injection', {
            'mode': 'PUBLIC_FEED_DISCONNECT', 'after_messages': True, 'never_admit': True}))
        with self.assertRaisesRegex(SoakAuditError, 'Malformed controlled-fault'):
            audit(self.source)

    def test_missing_never_admit_rejected(self):
        self.mutate(lambda m: m.__setitem__('controlled_fault_injection', {
            'mode': 'PUBLIC_FEED_DISCONNECT', 'after_messages': 7}))
        with self.assertRaisesRegex(SoakAuditError, 'Malformed controlled-fault'):
            audit(self.source)

    def test_unknown_injection_mode_rejected(self):
        self.mutate(lambda m: m.__setitem__('controlled_fault_injection', {
            'mode': 'TRADE', 'after_messages': 7, 'never_admit': True}))
        with self.assertRaisesRegex(SoakAuditError, 'Malformed controlled-fault'):
            audit(self.source)


@unittest.skipUnless(shutil.which('node'), 'Node 22+ required')
class SimulatedDisconnectedMarketFeed(unittest.TestCase):
    def test_unreached_disconnect_threshold_stays_unverified(self):
        # A fault-test requested but never triggered must not masquerade as
        # an observed interruption or a validated clean source recording.
        with tempfile.TemporaryDirectory() as d:
            env = os.environ.copy()
            env.update({'SOAK_DURATION_SECONDS': '15',
                        'SOAK_TEST_DISCONNECT_AFTER_MESSAGES': '100000',
                        'SOAK_DISK_RESERVE_BYTES': '0',
                        'SOAK_CHUNK_LIMIT_BYTES': '1024',
                        'SOAK_RAW_BUDGET_BYTES': '1000000',
                        'SOAK_ARCHIVE_BUDGET_BYTES': '1200000'})
            result = subprocess.run([
                'node', '--import', str(ROOT / 'tests/mock_coinbase_periodic.mjs'),
                str(ROOT / 'scripts/btc_eth_rolling_v14.mjs')],
                cwd=d, env=env, capture_output=True, text=True, timeout=24)
            self.assertEqual(result.returncode, 2)
            archives = list((Path(d) / 'soak_evidence').glob('coinbase_btc_eth_soak_*'))
            self.assertEqual(len(archives), 1)
            manifest = json.loads((archives[0] / 'manifest.json').read_text())
            self.assertTrue(manifest['injection_not_triggered'])
            self.assertEqual(manifest['status'], 'CAPTURE_INCOMPLETE_OR_INVALID')
            self.assertEqual(manifest['total_reconnects'], 0)
            self.assertFalse(manifest['ready_for_research'])
            self.assertEqual(audit(archives[0])['status'], 'ARCHIVED_BUT_NOT_ADMITTED')

    def test_one_disconnection_quarantines_entire_observation(self):
        with tempfile.TemporaryDirectory() as d:
            env = os.environ.copy()
            env.update({'SOAK_DURATION_SECONDS': '15',
                        'SOAK_TEST_DISCONNECT_AFTER_MESSAGES': '7',
                        'SOAK_DISK_RESERVE_BYTES': '0',
                        'SOAK_CHUNK_LIMIT_BYTES': '1024',
                        'SOAK_RAW_BUDGET_BYTES': '1000000',
                        'SOAK_ARCHIVE_BUDGET_BYTES': '1200000'})
            result = subprocess.run([
                'node', '--import', str(ROOT / 'tests/mock_coinbase_periodic.mjs'),
                str(ROOT / 'scripts/btc_eth_rolling_v14.mjs')],
                cwd=d, env=env, capture_output=True, text=True, timeout=24)
            self.assertEqual(result.returncode, 2, result.stdout[-700:] + result.stderr[-700:])
            sources = list((Path(d) / 'soak_evidence').glob('coinbase_btc_eth_soak_*'))
            self.assertEqual(len(sources), 1)
            source = sources[0]
            manifest = json.loads((source / 'manifest.json').read_text())
            self.assertFalse(manifest['ready_for_research'])
            self.assertEqual(manifest['status'], 'CAPTURE_COMPLETED_WITH_QUARANTINED_GAPS')
            self.assertEqual(manifest['total_reconnects'], 1)
            self.assertEqual(len(manifest['connections']), 2)
            self.assertTrue(manifest['connections'][0]['integrity_fault'])
            self.assertFalse(manifest['connections'][1]['integrity_fault'])
            self.assertTrue(any('INJECTED_DISCONNECT_AFTER_COMMITTED_SOURCE' in x['reason']
                                for x in manifest['faults']))
            self.assertTrue(all(all(conn['snapshots'].values()) for conn in manifest['connections']))
            first = audit(source)
            second = replay(source)
            self.assertEqual(first['status'], 'ARCHIVED_BUT_NOT_ADMITTED')
            self.assertFalse(first['eligible_for_point_in_time_feature_research'])
            self.assertEqual(first['connection_sequences']['0'], [0, 6])
            self.assertEqual(first['connection_sequences']['1'][0], 0)
            self.assertEqual(first['books_final'], second['books_final'])
            self.assertEqual(first['per_product']['BTC-USD']['unique_trade_id'], 2)
            self.assertFalse(first['orders_enabled'])


if __name__ == '__main__':
    unittest.main()
