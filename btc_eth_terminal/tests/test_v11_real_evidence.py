"""Mandatory source-archived 180-second independent market replay parity.

Expected counts/hash apply to one immutable collected 2026-11-14 UTC sample,
not to today's prices, venue-signed order books, trading costs or alpha.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
sys.path.insert(0,str(ROOT/'tests'))
from audit_soak import audit, SoakAuditError
from independent_soak_replay import replay, IndependentReplayError
from quote_spread_audit import inspect
from test_v10_soak import create_archive

REAL=ROOT/'backend/data/live_chunked_180s_20261114/coinbase_btc_eth_soak_5XeK6R'
SECOND=ROOT/'backend/data/live_chunked_reverified_180s_20261114/coinbase_btc_eth_soak_5BJxvi'


class RealChunked180SecValidation(unittest.TestCase):
    def test_real_replay_matches_independent_entire_book(self):
        self.assertTrue(REAL.is_dir(), 'Mandatory real raw archive missing')
        primary=audit(REAL)
        secondary=replay(REAL)
        self.assertEqual(primary['status'],'INTERNALLY_REPLAYED_NO_KNOWN_GAP')
        self.assertEqual(primary['message_count'],7215)
        self.assertEqual(primary['chunk_count'],3)
        self.assertEqual(primary['connection_sequences'],{'0':[0,7214]})
        self.assertEqual(secondary['messages'],primary['message_count'])
        self.assertEqual(secondary['chunks'],primary['chunk_count'])
        self.assertEqual(secondary['books_final'],primary['books_final'])
        for p, trades, updates in (('BTC-USD',1903,3152),('ETH-USD',630,3068)):
            self.assertEqual(primary['per_product'][p]['unique_trade_id'],trades)
            self.assertEqual(primary['per_product'][p]['update'],updates)
            for key,val in secondary['per_product'][p].items():
                self.assertEqual(primary['per_product'][p].get(key,0),val,(p,key))
        self.assertFalse(primary['executable_alpha_verified'])
        self.assertFalse(secondary['source_authentication_verified'])

    def test_second_independent_live_producer_revision_replay(self):
        self.assertTrue(SECOND.is_dir(), 'Mandatory independent second run missing')
        primary=audit(SECOND)
        secondary=replay(SECOND)
        self.assertEqual(primary['message_count'],secondary['messages'])
        self.assertEqual(primary['message_count'],8742)
        self.assertEqual(primary['chunk_count'],7)
        self.assertEqual(primary['connection_sequences'],{'0':[0,8741]})
        self.assertEqual(primary['status'],'INTERNALLY_REPLAYED_NO_KNOWN_GAP')
        self.assertEqual(primary['books_final'],secondary['books_final'])
        for p in ('BTC-USD','ETH-USD'):
            for k,v in secondary['per_product'][p].items():
                self.assertEqual(primary['per_product'][p].get(k,0),v,(p,k))
        self.assertEqual(primary['per_product']['BTC-USD']['identical_update_trade_replays_excluded'],11)
        self.assertFalse(secondary['executable_alpha_verified'])

    def test_real_quote_spread_is_not_fill_or_fee_estimate(self):
        report=inspect(REAL,step_seconds=5,max_quote_age_seconds=3)
        self.assertEqual(report['status'],'OBSERVED_QUOTES_ONLY_NO_FILL_PROOF')
        self.assertEqual(report['source_messages'],7215)
        self.assertIsNone(report['user_supplied_hypothetical_one_way_taker_fee_bp'])
        self.assertFalse(report['actual_order_fills_verified'])
        self.assertFalse(report['historical_fee_tier_verified'])
        self.assertGreater(report['products']['BTC-USD']['fresh_quote_samples'],20)
        self.assertGreater(report['products']['ETH-USD']['fresh_quote_samples'],20)


class IndependentImplementationPoisonPills(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)/'fixture'
        create_archive(self.folder)

    def test_synthetic_fixture_has_matching_final_books(self):
        primary=audit(self.folder)
        secondary=replay(self.folder)
        self.assertEqual(primary['books_final'],secondary['books_final'])
        self.assertEqual(primary['message_count'],secondary['messages'])

    def test_swapped_raw_payload_rejected_by_both_auditors(self):
        f=self.folder/'chunks/chunk_00000_conn_000.jsonl'
        changed=f.read_bytes().replace(b'BTC-USD',b'ETH-USD',1)
        f.write_bytes(changed)
        with self.assertRaises(SoakAuditError):audit(self.folder)
        with self.assertRaises(IndependentReplayError):replay(self.folder)

    def test_missing_chunk_is_rejected(self):
        (self.folder/'chunks/chunk_00001_conn_000.jsonl').unlink()
        with self.assertRaises(SoakAuditError):audit(self.folder)
        with self.assertRaises((OSError,IndependentReplayError)):replay(self.folder)

if __name__=='__main__':
    unittest.main()
