"""v23: causality, microstructure sanity, and blind split falsification."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import json
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from v23_causal_microstructure import (FeatureReject, Book, ofi_delta,
    features_from_events, create_report)
from v23_prereg_guard import (ProtocolReject, read_json, validate_protocol,
    evaluate_structural_rows, lock_draft_once, verify_lock, utc_ms)

T = 1_800_000_000_000
P = ROOT / 'examples/v23_preregistration_DRAFT.json'


def change(side, price, qty):
    return {'side':side,'price_level':str(price),'new_quantity':str(qty)}


def events():
    return [
        (T, 1, 'BTC-USD', 'snapshot', [change('bid',100,10),change('offer',101,6)]),
        (T, 1, 'ETH-USD', 'snapshot', [change('bid',200,8),change('offer',201,8)]),
        (T+2, 2, 'BTC-USD', 'update', [change('bid',100,12)]),
        (T+3, 3, 'BTC-USD', 'update', [change('offer',101,4)]),
    ]


class CausalFeatures(unittest.TestCase):
    def test_mid_and_imbalance_before_after(self):
        r=features_from_events(events(),[T+1,T+3])
        a,b=r[0],r[2]
        self.assertEqual(Decimal(a['microprice']),Decimal('100.625'))
        self.assertEqual(Decimal(a['best_imbalance']),Decimal('0.25'))
        self.assertEqual(Decimal(a['ofi_rolling_5m_base']),0)
        self.assertEqual(Decimal(b['ofi_rolling_5m_base']),4)
        self.assertEqual(b['source_message_sequence'],3)

    def test_source_at_equal_clock_admitted(self):
        r=features_from_events(events(),[T+2])
        self.assertEqual(r[0]['source_message_sequence'],2)

    def test_future_messages_cannot_change_past_features(self):
        a=features_from_events(events(),[T+1])
        future=events()+[(T+10,4,'BTC-USD','update',[change('bid',100,999)])]
        b=features_from_events(future,[T+1])
        self.assertEqual(a,b)

    def test_initial_snapshot_no_ofi(self):
        r=features_from_events(events(),[T+1])
        self.assertEqual(Decimal(r[0]['ofi_rolling_5m_base']),0)

    def test_rising_bid_and_falling_ask_positive_ofi(self):
        old=((Decimal(100),Decimal(10)),(Decimal(101),Decimal(6)))
        new=((Decimal(100),Decimal(12)),(Decimal(101),Decimal(4)))
        self.assertEqual(ofi_delta(old,new),4)

    def test_bid_price_drop_removes_prior_size(self):
        old=((Decimal(100),Decimal(12)),(Decimal(101),Decimal(6)))
        new=((Decimal(99),Decimal(8)),(Decimal(101),Decimal(6)))
        self.assertEqual(ofi_delta(old,new),-12)

    def test_quote_crossing_rejected(self):
        arr=events()+[(T+4,4,'BTC-USD','update',[change('bid',101,20)])]
        with self.assertRaisesRegex(FeatureReject,'Crossed'):
            features_from_events(arr,[T+10])

    def test_update_without_snapshot_rejected(self):
        with self.assertRaises(FeatureReject):
            features_from_events([(T,1,'BTC-USD','update',[change('bid',10,1)])],[T+1])

    def test_second_snapshot_rejected(self):
        arr=events()+[(T+5,4,'BTC-USD','snapshot',[]) ]
        with self.assertRaisesRegex(FeatureReject,'Duplicate snapshot'):
            features_from_events(arr,[T+10])

    def test_invalid_side_rejected(self):
        arr=events()+[(T+4,4,'BTC-USD','update',[change('BUY',100,2)])]
        with self.assertRaisesRegex(FeatureReject,'side'):
            features_from_events(arr,[T+10])

    def test_negative_size_rejected(self):
        arr=events()+[(T+4,4,'BTC-USD','update',[change('bid',100,-1)])]
        with self.assertRaisesRegex(FeatureReject,'price or size'):
            features_from_events(arr,[T+10])

    def test_out_of_order_time_rejected(self):
        arr=events()+[(T+1,4,'BTC-USD','update',[]) ]
        with self.assertRaisesRegex(FeatureReject,'chronology'):
            features_from_events(arr,[T+10])

    def test_out_of_order_sequence_rejected(self):
        arr=events()+[(T+5,2,'BTC-USD','update',[]) ]
        with self.assertRaisesRegex(FeatureReject,'chronology'):
            features_from_events(arr,[T+10])

    def test_other_market_rejected(self):
        arr=events()+[(T+5,4,'DOGE-USD','update',[]) ]
        with self.assertRaises(FeatureReject):
            features_from_events(arr,[T+10])

    def test_staleness_not_valid(self):
        r=features_from_events(events(),[T+6000])
        self.assertTrue(all(x['status']=='NO_ADMISSIBLE_QUOTE' for x in r))
        self.assertTrue(all('microprice' not in x for x in r))

    def test_no_lookahead_around_decision(self):
        base=features_from_events(events(),[T+1])
        changed=events()+[(T+100,4,'ETH-USD','update',[change('offer',201,999)])]
        self.assertEqual(base,features_from_events(changed,[T+1]))

    def test_old_ofi_expires_without_future(self):
        # Allow an intentionally large quote age solely for toy window test.
        r=features_from_events(events(),[T+2,T+300003],max_age_ms=400000)
        self.assertEqual(Decimal(r[0]['ofi_rolling_5m_base']),2)
        self.assertEqual(Decimal(r[2]['ofi_rolling_5m_base']),0)

    def test_microprice_typed_not_label(self):
        row=features_from_events(events(),[T+1])[0]
        self.assertFalse(row['eligible_for_oos_or_trading'])
        self.assertFalse(row['original_feature_label_pit_verified'])
        self.assertNotIn('future_label',row)

    def test_real_source_baseline_parity(self):
        x=create_report(ROOT/'backend/data/live_v16_one_hour/coinbase_btc_eth_soak_eE9SNH',
              json.loads((ROOT/'V21_SOURCE_EVIDENCE_REPORT.json').read_text()))
        self.assertEqual(x['source_message_count'],152069)
        self.assertEqual(len(x['results']),24)
        self.assertFalse(x['eligible_for_oos_or_execution'])
        expected=json.loads((ROOT/'V23_CAUSAL_MICROSTRUCTURE_DIAGNOSTIC.json').read_text())
        self.assertEqual(x,expected)


class PreregTests(unittest.TestCase):
    def setUp(self):
        self.p = json.loads(P.read_text())

    def test_draft_structural_only(self):
        self.assertIn('blind',validate_protocol(self.p))
        self.assertFalse(self.p['original_source_pit_verified'])

    def test_no_approved_source_spoof(self):
        self.p['original_source_pit_verified']=True
        with self.assertRaises(ProtocolReject):validate_protocol(self.p)

    def test_no_long_run_spoof(self):
        self.p['independent_multi_day_data_verified']=True
        with self.assertRaises(ProtocolReject):validate_protocol(self.p)

    def test_no_blind_peek(self):
        self.p['blind_results_inspected']=True
        with self.assertRaises(ProtocolReject):validate_protocol(self.p)

    def test_budget_bounded(self):
        self.p['trial_budget']=1_000_000
        with self.assertRaises(ProtocolReject):validate_protocol(self.p)

    def test_embargo_not_shorter_than_labels(self):
        self.p['embargo_ms']=1000
        with self.assertRaises(ProtocolReject):validate_protocol(self.p)

    def test_overlap_fails(self):
        self.p['partition_schedule']['validation']['start']='2027-01-07T00:00:00Z'
        with self.assertRaises(ProtocolReject):validate_protocol(self.p)

    def test_venue_fingerprint_not_parent_one_hour(self):
        self.p['source_identifier']='08255d79f0a7f081921f370feca2e92686cde3dffbb406eadc23e918d43e73d7'
        with self.assertRaises(ProtocolReject):validate_protocol(self.p)

    def test_naughty_baseline_rejected(self):
        self.p['baselines'].append('OPTIMIZED_ON_BLIND')
        with self.assertRaises(ProtocolReject):validate_protocol(self.p)

    def test_naughty_feature_rejected(self):
        self.p['features'].append('FUTURE_RETURN')
        with self.assertRaises(ProtocolReject):validate_protocol(self.p)

    def test_invalid_utc_is_rejected(self):
        with self.assertRaises(ProtocolReject):utc_ms('2027-01-01 00:00:00')

    def test_duplicate_json_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'x.json'
            p.write_text('{"a":1,"a":2}')
            with self.assertRaises(ProtocolReject):read_json(p)

    def test_local_draft_lock_immutable(self):
        with tempfile.TemporaryDirectory() as td:
            dst=Path(td)/'lock.json'
            digest=lock_draft_once(P,dst)
            self.assertEqual(digest,verify_lock(dst))
            with self.assertRaises(ProtocolReject):lock_draft_once(P,dst)
            altered=json.loads(dst.read_text())
            altered['protocol']['trial_budget']=100
            dst.write_text(json.dumps(altered))
            with self.assertRaises(ProtocolReject):verify_lock(dst)

    def row(self,part,horizon=5000,near_end=False,blind_label=False):
        a,b=validate_protocol(self.p)[part]
        t=b-1000 if near_end else a+100000
        row={'instrument':'BTC-USD','market_type':'SPOT','source_type':'SYNTHETIC_TEST_ONLY',
           'decision_ms':t,'feature_max_received_ms':t-1000,'label_end_ms':t+horizon,
           'horizon_ms':horizon,'source_seq':1000}
        if part!='blind' or blind_label:
            row['future_label_value']=0.5
        return row

    def test_partition_counts_no_blind_labels(self):
        x=evaluate_structural_rows([self.row('train'),self.row('validation'),self.row('blind')],self.p)
        self.assertEqual((x['train_rows'],x['validation_rows'],x['blind_metadata_rows']),(1,1,1))
        self.assertFalse(x['blind_label_read_or_evaluated'])
        self.assertFalse(x['eligible_for_real_research_promotion'])

    def test_blind_labels_forbidden(self):
        with self.assertRaisesRegex(ProtocolReject,'Blind labels'):
            evaluate_structural_rows([self.row('blind',blind_label=True)],self.p)

    def test_label_boundary_purged(self):
        x=evaluate_structural_rows([self.row('validation',near_end=True)],self.p)
        self.assertEqual(x['purged_rows'],1)
        self.assertEqual(x['validation_rows'],0)

    def test_future_feature_rejected(self):
        row=self.row('train')
        row['feature_max_received_ms']=row['decision_ms']+1
        with self.assertRaises(ProtocolReject):evaluate_structural_rows([row],self.p)

    def test_wrong_horizon_rejected(self):
        row=self.row('train')
        row['label_end_ms']+=1
        with self.assertRaises(ProtocolReject):evaluate_structural_rows([row],self.p)

    def test_duplicate_records_rejected(self):
        r=self.row('train')
        with self.assertRaises(ProtocolReject):evaluate_structural_rows([r,r],self.p)

    def test_unknown_source_rejected(self):
        row=self.row('train')
        row['source_type']='REAL_COINBASE'
        with self.assertRaises(ProtocolReject):evaluate_structural_rows([row],self.p)

    def test_unknown_field_rejected(self):
        row=self.row('train')
        row['future_price_forecast_exploited']=100
        with self.assertRaises(ProtocolReject):evaluate_structural_rows([row],self.p)


if __name__ == '__main__':unittest.main()
