"""Synthetic-only negative controls, not an audit of absent user research data."""
import csv
from hashlib import sha256
import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from original_pair_audit_v20 import audit, ProvenanceError, text_json

START=1794096000000
STEP=300000


def fhash(p):
    return sha256(p.read_bytes()).hexdigest()


class SourceAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.fname='btc_features_5m.csv.gz'
        self.lname='btc_future_labels.csv.gz'
        self.rows=3
        self.feature_headers=['bar','decision','observed','available','signal']
        self.label_headers=['bar','decision','horizon_end','available','future_return']
        self.features=[]
        self.labels=[]
        for i in range(self.rows):
            bar=START+i*STEP
            d=bar+STEP
            self.features.append([str(bar),str(d),str(d-1000),str(d),'0.1'])
            self.labels.append([str(bar),str(d),str(d+STEP),str(d+STEP),'0.12'])
        self.save()

    def save(self):
        with gzip.open(self.root/self.fname,'wt',encoding='utf-8',newline='') as f:
            writer=csv.writer(f);writer.writerow(self.feature_headers);writer.writerows(self.features)
        with gzip.open(self.root/self.lname,'wt',encoding='utf-8',newline='') as f:
            writer=csv.writer(f);writer.writerow(self.label_headers);writer.writerows(self.labels)
        self.manifest={'schema':'btc_eth_original_research_pair_v20','source_provider':'DECLARED_TEST_FIXTURE',
          'venue':'Coinbase','market_type':'spot','product_id':'BTC-USD','bar_seconds':300,'timestamp_format':'epoch_ms',
          'feature':{'path':self.fname,'sha256':fhash(self.root/self.fname),'bar_open_column':'bar','decision_at_column':'decision',
                     'observed_at_column':'observed','available_at_column':'available'},
          'future_labels':{'path':self.lname,'sha256':fhash(self.root/self.lname),'bar_open_column':'bar',
                          'decision_at_column':'decision','available_at_column':'available',
                          'label_end_column':'horizon_end','horizon_ms':STEP}}

    def test_explicit_pair_is_still_unattested(self):
        result=audit(self.root,self.manifest)
        self.assertEqual(result['status'],'STRUCTURAL_PAIR_AUDIT_ONLY_NOT_PIT_ATTESTATION')
        self.assertFalse(result['eligible_for_OOS_research_without_independent_provenance'])
        self.assertFalse(result['feature_availability_original_capture_verified'])

    def test_corruption_rejected(self):
        self.features[0][-1]='0.2'
        self.save()
        self.manifest['feature']['sha256']='0'*64
        with self.assertRaises(ProvenanceError):audit(self.root,self.manifest)

    def test_feature_future_data_at_decision_rejected(self):
        self.features[1][3]=str(START+STEP*2+1)
        self.save()
        with self.assertRaisesRegex(ProvenanceError,'Future-available'):
            audit(self.root,self.manifest)

    def test_feature_future_observation_rejected(self):
        self.features[0][2]=str(START+STEP+5)
        self.save()
        with self.assertRaisesRegex(ProvenanceError,'Future-observed'):
            audit(self.root,self.manifest)

    def test_wrong_label_horizon_rejected(self):
        self.labels[1][2]=str(START+3*STEP+500)
        self.save()
        with self.assertRaisesRegex(ProvenanceError,'Label end differs'):
            audit(self.root,self.manifest)

    def test_label_available_before_end_rejected(self):
        self.labels[0][3]=str(START+STEP+100)
        self.save()
        with self.assertRaisesRegex(ProvenanceError,'Label claims availability'):
            audit(self.root,self.manifest)

    def test_label_cohort_timeline_mismatch_rejected(self):
        self.labels[1][1]=str(START+STEP*2+1)
        self.labels[1][2]=str(START+STEP*3+1)
        self.labels[1][3]=str(START+STEP*3+1)
        self.save()
        with self.assertRaisesRegex(ProvenanceError,'row cohort mismatch'):
            audit(self.root,self.manifest)

    def test_future_label_header_in_features_rejected(self):
        self.feature_headers[-1]='future_return'
        self.save()
        with self.assertRaisesRegex(ProvenanceError,'future-target'):
            audit(self.root,self.manifest)

    def test_venue_product_contradiction_rejected(self):
        self.manifest['venue']='Binance'
        with self.assertRaisesRegex(ProvenanceError,'Binance source'):
            audit(self.root,self.manifest)

    def test_symlinked_feature_rejected(self):
        self.manifest['feature']['path']='redirect_features_5m.csv.gz'
        (self.root/'redirect_features_5m.csv.gz').symlink_to(self.root/self.fname)
        with self.assertRaisesRegex(ProvenanceError,'Symlink'):
            audit(self.root,self.manifest)

    def test_duplicate_header_rejected(self):
        self.feature_headers[-1]='bar'
        self.save()
        with self.assertRaisesRegex(ProvenanceError,'Ambiguous feature'):
            audit(self.root,self.manifest)

    def test_no_label_file_rejected(self):
        (self.root/self.lname).unlink()
        with self.assertRaisesRegex(ProvenanceError,'Missing/out-of-root'):
            audit(self.root,self.manifest)

if __name__=='__main__':unittest.main()
