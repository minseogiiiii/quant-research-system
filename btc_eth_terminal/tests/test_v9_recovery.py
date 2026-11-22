"""Fault-injection and real-source gates for the v9 controlled reconnect pilot."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from l2_recovery_gate import BookAdmissionGate, RecoveryRequired
from audit_l2_reconnect import audit_reconnect, StrictL2Error

REAL = ROOT / 'backend' / 'data' / 'reconnect_evidence'


def message(seq, channel='subscriptions', events=None):
    return {'sequence_num': seq, 'channel': channel,
            'events': events if events is not None else []}


def book_event(product, kind):
    return {'product_id': product, 'type': kind, 'updates': []}


class RecoveryGateTests(unittest.TestCase):
    def setUp(self):
        self.g = BookAdmissionGate()

    def test_no_source_accepted_while_disconnected(self):
        with self.assertRaises(RecoveryRequired):
            self.g.consume(message(0))

    def test_requires_both_fresh_snapshots(self):
        self.g.connected()
        self.assertFalse(self.g.consume(message(0)))
        self.assertFalse(self.g.consume(message(1, 'l2_data',
                                                [book_event('BTC-USD','snapshot')])))
        self.assertTrue(self.g.consume(message(2, 'l2_data',
                                               [book_event('ETH-USD','snapshot')])))
        self.assertEqual(self.g.completed_connections, 1)

    def test_update_before_its_own_snapshot_forces_resync(self):
        self.g.connected()
        self.g.consume(message(0, 'l2_data', [book_event('BTC-USD', 'snapshot')]))
        with self.assertRaisesRegex(RecoveryRequired, 'before initial snapshot'):
            self.g.consume(message(1, 'l2_data', [book_event('ETH-USD', 'update')]))
        self.assertEqual(self.g.state, 'RESYNC_REQUIRED')
        with self.assertRaises(RecoveryRequired):
            self.g.consume(message(2))

    def test_loss_of_single_message_disables_admission_until_new_connection(self):
        self.g.connected()
        self.g.consume(message(0, 'l2_data', [book_event('BTC-USD','snapshot')]))
        self.g.consume(message(1, 'l2_data', [book_event('ETH-USD','snapshot')]))
        self.assertTrue(self.g.ready)
        with self.assertRaises(RecoveryRequired):
            self.g.consume(message(3))
        self.assertFalse(self.g.ready)
        with self.assertRaises(RecoveryRequired):
            self.g.connected()
        self.g.disconnected()
        self.g.connected()
        self.assertFalse(self.g.consume(message(0)))
        self.assertFalse(self.g.consume(message(1, 'l2_data', [book_event('BTC-USD','snapshot')])))
        self.assertTrue(self.g.consume(message(2, 'l2_data', [book_event('ETH-USD','snapshot')])))

    def test_repeated_message_sequence_fails(self):
        self.g.connected()
        self.g.consume(message(0))
        with self.assertRaises(RecoveryRequired):
            self.g.consume(message(0))

    def test_duplicate_snapshot_is_error(self):
        self.g.connected()
        self.g.consume(message(0,'l2_data',[book_event('BTC-USD','snapshot')]))
        with self.assertRaises(RecoveryRequired):
            self.g.consume(message(1,'l2_data',[book_event('BTC-USD','snapshot')]))

    def test_wrong_product_is_error(self):
        self.g.connected()
        with self.assertRaises(RecoveryRequired):
            self.g.consume(message(0,'l2_data',[book_event('QQQ','snapshot')]))

    def test_reset_drops_readiness_and_old_snapshots(self):
        self.g.connected()
        self.g.consume(message(0,'l2_data',[book_event('BTC-USD','snapshot')]))
        self.g.consume(message(1,'l2_data',[book_event('ETH-USD','snapshot')]))
        self.assertTrue(self.g.ready)
        self.g.disconnected()
        self.assertFalse(self.g.ready)
        self.assertEqual(self.g.snapshots,set())
        self.g.connected()
        self.assertFalse(self.g.consume(message(0)))
        self.assertEqual(self.g.completed_connections,1)

    def test_invalid_bool_sequence_rejected(self):
        self.g.connected()
        with self.assertRaises(RecoveryRequired):
            self.g.consume(message(True))

    def test_reconnect_without_close_rejected(self):
        self.g.connected()
        with self.assertRaises(RecoveryRequired):
            self.g.connected()


class RealRecoveryEvidenceTests(unittest.TestCase):
    def _copy(self, out):
        # Use copied data only. Never mutate the source recording under backend/data.
        shutil.copytree(REAL, out, dirs_exist_ok=True)

    def test_real_reconnect_and_four_reference_books(self):
        x = audit_reconnect(REAL)
        self.assertEqual(x['book_admission_gate_completed_connections'],2)
        self.assertEqual(x['book_admission_gate_invalidated_connections'],2)
        self.assertEqual([s['records'] for s in x['segments']],[626,629])
        for s in x['segments']:
            self.assertEqual(s['connection_seq_first'],0)
            self.assertEqual(s['recovery_admission_gate']['state_after_disconnect'],'DISCONNECTED')
            self.assertEqual(s['recovery_admission_gate']['messages_until_both_fresh_snapshots'],8)
            for ref in s['reference_books']:
                self.assertEqual(ref['availability'],'ARCHIVED_SOURCE_VALID')
                self.assertTrue(ref['comparison']['rest_bid_in_observed_ws_range'])
                self.assertTrue(ref['comparison']['rest_ask_in_observed_ws_range'])
                self.assertFalse(ref['comparison']['atomic_book_equality_established'])

    def test_incorrect_status_hash_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._copy(Path(tmp))
            path=Path(tmp)/'segment_after_reconnect'/'capture_status.json'
            obj=json.loads(path.read_text())
            obj['records'] += 1
            path.write_text(json.dumps(obj))
            with self.assertRaisesRegex(StrictL2Error,'hash mismatch'):
                audit_reconnect(Path(tmp))

    def test_rest_raw_digest_mutation_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._copy(Path(tmp))
            raw=Path(tmp)/'segment_before_disconnect/reference_books/BTC_USD.json'
            raw.write_bytes(raw.read_bytes()+b' ')
            with self.assertRaisesRegex(StrictL2Error,'digest'):
                audit_reconnect(Path(tmp))

    def test_rest_forged_product_even_with_recomputed_hash_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._copy(Path(tmp))
            d=Path(tmp)/'segment_before_disconnect/reference_books'
            path=d/'BTC_USD.json'; meta=d/'BTC_USD.meta.json'
            doc=json.loads(path.read_bytes())
            doc['pricebook']['product_id']='DOGE-USD'
            raw=json.dumps(doc).encode();path.write_bytes(raw)
            obj=json.loads(meta.read_text());obj['raw_sha256']=hashlib.sha256(raw).hexdigest()
            obj['raw_bytes']=len(raw);meta.write_text(json.dumps(obj))
            with self.assertRaisesRegex(StrictL2Error,'mislabeled'):
                audit_reconnect(Path(tmp))

    def test_rest_future_exchange_time_with_recomputed_hash_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._copy(Path(tmp))
            d=Path(tmp)/'segment_before_disconnect/reference_books'
            path=d/'ETH_USD.json';meta=d/'ETH_USD.meta.json'
            doc=json.loads(path.read_bytes())
            doc['pricebook']['time']='2099-01-01T00:00:00Z'
            raw=json.dumps(doc).encode();path.write_bytes(raw)
            obj=json.loads(meta.read_text());obj['raw_sha256']=hashlib.sha256(raw).hexdigest()
            obj['raw_bytes']=len(raw);meta.write_text(json.dumps(obj))
            with self.assertRaisesRegex(StrictL2Error,'timestamp implausible'):
                audit_reconnect(Path(tmp))

    def test_reordered_parent_segments_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._copy(Path(tmp))
            path=Path(tmp)/'reconnect_summary.json'
            obj=json.loads(path.read_text());obj['segments'].reverse()
            path.write_text(json.dumps(obj))
            with self.assertRaisesRegex(StrictL2Error,'ordered'):
                audit_reconnect(Path(tmp))

    def test_source_close_proof_cannot_be_changed_even_with_status_hash_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._copy(Path(tmp))
            s=Path(tmp)/'segment_after_reconnect/capture_status.json'
            obj=json.loads(s.read_text());obj['controlled_disconnect_requested']=False
            raw=json.dumps(obj).encode();s.write_bytes(raw)
            root=Path(tmp)/'reconnect_summary.json';m=json.loads(root.read_text())
            m['segments'][1]['status_sha256']=hashlib.sha256(raw).hexdigest()
            root.write_text(json.dumps(m))
            with self.assertRaisesRegex(StrictL2Error,'controlled'):
                audit_reconnect(Path(tmp))


if __name__=='__main__':
    unittest.main()
