"""Adversarial, synthetic negative tests for immutable source cohorts.

A successful engineering test is never evidence of executable alpha.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from v28_cohort_ledger import (
    CohortLedgerError, SOURCE_PINS, CRITICAL_REPLAY_TARGETS, utc_time,
    validate_pin, overlapping_pairs, verify_chunk_inventory, cohort_summary,
)

PROJECT_ROOT=Path(__file__).resolve().parents[1]

class TestV28TemporalLedger(unittest.TestCase):
    def sample(self,a,b,label='a'):
        return {'start_utc':a,'end_utc':b,'source_path':label}

    def test_01_utc_exact(self):
        self.assertEqual(utc_time('2026-11-15T00:00:00Z').year,2026)
    def test_02_timezone_offset_forbidden(self):
        with self.assertRaises(CohortLedgerError): utc_time('2026-11-15T00:00:00+00:00')
    def test_03_naive_forbidden(self):
        with self.assertRaises(CohortLedgerError): utc_time('2026-11-15T00:00:00')
    def test_04_invalid_date_forbidden(self):
        with self.assertRaises(CohortLedgerError): utc_time('2026-02-31T00:00:00Z')
    def test_05_nonstr_forbidden(self):
        with self.assertRaises(CohortLedgerError): utc_time(17)
    def test_06_overlap_detected(self):
        a=[self.sample('2026-11-15T00:00:00Z','2026-11-15T00:00:08Z'),
           self.sample('2026-11-15T00:00:07Z','2026-11-15T00:00:11Z','b')]
        self.assertEqual(overlapping_pairs(a)[0]['overlap_seconds'],1.0)
    def test_07_touching_half_open_not_overlap(self):
        a=[self.sample('2026-11-15T00:00:00Z','2026-11-15T00:00:08Z'),
           self.sample('2026-11-15T00:00:08Z','2026-11-15T00:00:11Z','b')]
        self.assertEqual(overlapping_pairs(a),[])
    def test_08_nested_window_overlap(self):
        a=[self.sample('2026-11-15T00:00:00Z','2026-11-15T00:01:00Z'),
           self.sample('2026-11-15T00:00:08Z','2026-11-15T00:00:11Z','b')]
        self.assertEqual(overlapping_pairs(a)[0]['overlap_seconds'],3.0)
    def test_09_backward_interval_refused(self):
        a=[self.sample('2026-11-15T00:00:08Z','2026-11-15T00:00:07Z')]
        with self.assertRaises(CohortLedgerError): overlapping_pairs(a)
    def test_10_zero_duration_refused(self):
        a=[self.sample('2026-11-15T00:00:08Z','2026-11-15T00:00:08Z')]
        with self.assertRaises(CohortLedgerError): overlapping_pairs(a)

class TestV28PinnedArchive(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.source=self.root/'safe'/'coinbase_btc_eth_soak_test'
        (self.source/'chunks').mkdir(parents=True)
        self.raw=b'{"connection_id":0,"raw_text":"x"}\n'
        self.chunk=self.source/'chunks/chunk_00000_conn_000.jsonl'
        self.chunk.write_bytes(self.raw)
        self.m={'sampled_from_utc':'2026-11-15T00:00:00Z',
                'finished_at_utc':'2026-11-15T00:00:01Z',
                'chunks':[{'file':'chunks/chunk_00000_conn_000.jsonl',
                           'archived_line_bytes':len(self.raw),
                           'archive_sha256':hashlib.sha256(self.raw).hexdigest()}]}
        self.manifest=self.source/'manifest.json'
        self.manifest.write_text(json.dumps(self.m))
        self.pin=hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        self.rel='safe/coinbase_btc_eth_soak_test'

    def test_11_pin_valid(self):
        self.assertEqual(validate_pin(self.root,self.rel,self.pin)[1],self.m)
    def test_12_manifest_changed_refused(self):
        self.manifest.write_text(self.manifest.read_text()+' ')
        with self.assertRaises(CohortLedgerError):validate_pin(self.root,self.rel,self.pin)
    def test_13_root_traversal_refused(self):
        with self.assertRaises(CohortLedgerError):validate_pin(self.root,'safe/../coinbase_btc_eth_soak_test',self.pin)
    def test_14_absolute_path_refused(self):
        with self.assertRaises(CohortLedgerError):validate_pin(self.root,str(self.source.resolve()),self.pin)
    def test_15_backslash_refused(self):
        with self.assertRaises(CohortLedgerError):validate_pin(self.root,'safe\\bad',self.pin)
    def test_16_symlink_source_refused(self):
        (self.root/'link').symlink_to(self.source,target_is_directory=True)
        with self.assertRaises(CohortLedgerError):validate_pin(self.root,'link',self.pin)
    def test_17_symlink_manifest_refused(self):
        self.manifest.unlink();self.manifest.symlink_to(self.root/'unsafe.json')
        with self.assertRaises(CohortLedgerError):validate_pin(self.root,self.rel,self.pin)
    def test_18_source_member_sha_pass(self):
        self.assertEqual(verify_chunk_inventory(self.source,self.m),(1,len(self.raw)))
    def test_19_chunk_modified_refused(self):
        self.chunk.write_bytes(self.raw+b'x')
        with self.assertRaises(CohortLedgerError):verify_chunk_inventory(self.source,self.m)
    def test_20_chunk_different_same_length_refused(self):
        self.chunk.write_bytes(self.raw.replace(b'x',b'y'))
        with self.assertRaises(CohortLedgerError):verify_chunk_inventory(self.source,self.m)
    def test_21_extra_chunk_refused(self):
        (self.source/'chunks/chunk_00001_conn_000.jsonl').write_bytes(b'hello')
        with self.assertRaises(CohortLedgerError):verify_chunk_inventory(self.source,self.m)
    def test_22_duplicate_chunk_refused(self):
        self.m['chunks'].append(self.m['chunks'][0].copy())
        with self.assertRaises(CohortLedgerError):verify_chunk_inventory(self.source,self.m)
    def test_23_traversal_chunk_refused(self):
        self.m['chunks'][0]['file']='chunks/../../a.jsonl'
        with self.assertRaises(CohortLedgerError):verify_chunk_inventory(self.source,self.m)
    def test_24_chunk_symlink_refused(self):
        self.chunk.unlink();self.chunk.symlink_to(self.root/'unknown')
        with self.assertRaises(CohortLedgerError):verify_chunk_inventory(self.source,self.m)
    def test_25_missing_chunk_refused(self):
        self.chunk.unlink()
        with self.assertRaises(CohortLedgerError):verify_chunk_inventory(self.source,self.m)
    def test_26_archive_size_mismatch_refused(self):
        self.m['chunks'][0]['archived_line_bytes']+=1
        with self.assertRaises(CohortLedgerError):verify_chunk_inventory(self.source,self.m)
    def test_27_no_chunk_inventory_refused(self):
        self.m['chunks']=[]
        with self.assertRaises(CohortLedgerError):verify_chunk_inventory(self.source,self.m)
    def test_28_missing_root_refused(self):
        with self.assertRaises(CohortLedgerError):validate_pin(self.root,'no-such-source',self.pin)

class TestV28ActualSourceContracts(unittest.TestCase):
    def test_29_frozen_manifest_pins_unique(self):
        self.assertEqual(len(set(k for k,_ in SOURCE_PINS)),len(SOURCE_PINS))
        self.assertEqual(len(set(v for _,v in SOURCE_PINS)),len(SOURCE_PINS))
    def test_30_only_targeted_three_have_rereplay(self):
        self.assertEqual(len(CRITICAL_REPLAY_TARGETS),3)
    def test_31_existing_complete_manifest_and_chunk_digest(self):
        # Full SHA check of all fixed original archives; no expensive orderbook replay.
        a=cohort_summary(PROJECT_ROOT,replay_targets=set())
        self.assertEqual(a['cohort_count'],11)
        self.assertEqual(a['complete_chunk_sha_verified_cohorts'],11)
        self.assertEqual(a['double_replayed_cohort_count'],0)
        self.assertEqual(a['quarantined_cohorts'],1)
        self.assertEqual(a['overlapping_cohort_pairs'],2)
        self.assertEqual(a['independent_blind_oos_eligible_cohorts'],0)
        self.assertFalse(a['executable_alpha_confirmed'])
    def test_32_bad_pin_blocks_registry(self):
        rel,pin=SOURCE_PINS[0]
        with self.assertRaises(CohortLedgerError):cohort_summary(PROJECT_ROOT,
            pins=[(rel,'0'*64)],replay_targets=set())
    def test_33_duplicate_manifest_identity_blocks(self):
        rel,pin=SOURCE_PINS[0]
        with self.assertRaises(CohortLedgerError):cohort_summary(PROJECT_ROOT,
            pins=[(rel,pin),(rel,pin)],replay_targets=set())
    def test_34_unknown_target_blocks(self):
        with self.assertRaises(CohortLedgerError):cohort_summary(PROJECT_ROOT,
            pins=SOURCE_PINS,replay_targets={'unknown'})
    def test_35_source_immutable(self):
        p=PROJECT_ROOT/SOURCE_PINS[-1][0]/'manifest.json'
        self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),SOURCE_PINS[-1][1])

if __name__=='__main__': unittest.main()
