"""v22 false-positive rejection and fixed-source engineering regression tests."""
from __future__ import annotations
import copy
from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from v22_mid_exit_hurdle import (
    EvidenceReject,evaluate,load,dec,policy_checked,produce,
)
from v22_independent_hurdle_audit import IndependentMismatch,verify

class V22EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v17=load(ROOT/'P1_STATIC_DEPTH_STRESS_v17.json')
        cls.v18=load(ROOT/'P1_POST_QUOTE_MARKOUT_v18.json')
        cls.maker=load(ROOT/'V20_CANONICAL_MAKER_SCENARIOS.json')
        cls.v19a=load(ROOT/'P1_MAKER_QUEUE_SCENARIOS_v19.json')
        cls.policy=load(ROOT/'examples/v22_fixed_exit_scenarios.json')
        cls.source=cls.v17['source_manifest_sha256']
        cls.report=load(ROOT/'V22_FROZEN_EXECUTION_HURDLE.json')

    def reproduce(self,**changes):
        parts=dict(v17=copy.deepcopy(self.v17),v18=copy.deepcopy(self.v18),
                   maker=copy.deepcopy(self.maker),policy=copy.deepcopy(self.policy),
                   v19_a=copy.deepcopy(self.v19a))
        parts.update(changes)
        return evaluate(parts['v17'],parts['v18'],parts['maker'],parts['policy'],
            source_manifest_digest=self.source,input_shas={},v19_a=parts['v19_a'])

    def test_all_seven_frozen_and_21_future_labels_not_alphas(self):
        x=self.reproduce()
        self.assertEqual(len(x['results']),7)
        self.assertFalse(x['actual_fill_or_realized_pnl_verified'])
        self.assertFalse(x['alpha_or_out_of_sample_evidence_verified'])
        self.assertTrue(all(len(r['future_only_labels'])==3 for r in x['results']))
        self.assertEqual(sum(r['hypothetical_entry_reference_with_5bp_slippage'] is None
                             for r in x['results']),1)
        self.assertTrue(all(r['verified_real_maker_or_taker_fills'] is None for r in x['results']))
        self.assertEqual(x['results'],self.report['results'])

    def test_independent_second_implementation_real_report(self):
        audit=verify(ROOT,self.report)
        self.assertEqual(audit['scenario_comparisons'],63)
        self.assertEqual(audit['horizons_compared'],21)

    def test_changed_source_manifest(self):
        with self.assertRaisesRegex(EvidenceReject,'same audited source'):
            evaluate(self.v17,self.v18,self.maker,self.policy,
                     source_manifest_digest='1'*64,input_shas={},v19_a=self.v19a)

    def test_modified_future_label_asof_gate(self):
        x=copy.deepcopy(self.v18)
        x['future_labels_are_never_input_features']=False
        with self.assertRaises(EvidenceReject):self.reproduce(v18=x)

    def test_future_label_poisoned_midprice(self):
        x=copy.deepcopy(self.v18)
        x['results'][0]['labels'][0]['future_midprice']='1'
        with self.assertRaisesRegex(EvidenceReject,'signed midprice'):
            self.reproduce(v18=x)

    def test_future_label_source_sequence_not_after_quote(self):
        x=copy.deepcopy(self.v18)
        x['results'][0]['labels'][0]['label_observed_at_sequence']=1000
        with self.assertRaisesRegex(EvidenceReject,'label source sequence'):
            self.reproduce(v18=x)

    def test_future_label_received_before_quote(self):
        x=copy.deepcopy(self.v18)
        x['results'][0]['labels'][0]['label_available_at_received_utc']='2026-01-01T00:00:00.000Z'
        with self.assertRaisesRegex(EvidenceReject,'receive clock precedes'):
            self.reproduce(v18=x)

    def test_future_horizon_alignment_shift(self):
        x=copy.deepcopy(self.v18)
        x['results'][0]['labels'][0]['horizon_ms']=5000
        with self.assertRaisesRegex(EvidenceReject,'horizon drift'):
            self.reproduce(v18=x)

    def test_far_late_future_label_refused(self):
        x=copy.deepcopy(self.v18)
        x['results'][0]['labels'][0]['observed_delay_ms']='9999'
        with self.assertRaisesRegex(EvidenceReject,'not first timely'):
            self.reproduce(v18=x)

    def test_bad_side_matching(self):
        x=copy.deepcopy(self.maker)
        x['records'][0]['maker_side_at_passive_price']='SELL'
        with self.assertRaisesRegex(EvidenceReject,'anchor mismatch'):
            self.reproduce(maker=x)

    def test_quote_anchor_changed(self):
        x=copy.deepcopy(self.v18)
        x['results'][0]['quote_sequence']+=1
        with self.assertRaisesRegex(EvidenceReject,'anchor mismatch'):
            self.reproduce(v18=x)

    def test_historical_quote_anchor_pricing_changed(self):
        x=copy.deepcopy(self.v19a)
        x['results'][0]['posted_price_hypothetical']='123'
        with self.assertRaisesRegex(EvidenceReject,'v19-A quote anchor mismatch'):
            self.reproduce(v19_a=x)

    def test_no_duplicate_identifiers(self):
        x=copy.deepcopy(self.v17)
        x['results'][0]['id']=x['results'][1]['id']
        with self.assertRaises(EvidenceReject):self.reproduce(v17=x)

    def test_no_extra_anchor(self):
        x=copy.deepcopy(self.v18)
        x['results'].append(copy.deepcopy(x['results'][0]))
        with self.assertRaises(EvidenceReject):self.reproduce(v18=x)

    def test_invalid_maker_queue_arithmetic_rejected(self):
        x=copy.deepcopy(self.maker)
        x['records'][0]['time_windows_ms']['5000']['conditional_zero_ahead_quantity_no_impact']='1'
        with self.assertRaisesRegex(EvidenceReject,'Maker queue arithmetic'):
            self.reproduce(maker=x)

    def test_verified_fill_cannot_be_invented(self):
        x=copy.deepcopy(self.maker)
        x['records'][0]['time_windows_ms']['5000']['unverified_actual_fill_base']='0.0001'
        with self.assertRaisesRegex(EvidenceReject,'Actual maker fill'):
            self.reproduce(maker=x)

    def test_banned_positive_alpha_flag(self):
        x=copy.deepcopy(self.maker)
        x['sample_used_for_alpha_model_selection']=True
        with self.assertRaises(EvidenceReject):self.reproduce(maker=x)

    def test_static_depth_rejected_cannot_generate_cash_edge(self):
        x=self.reproduce()
        z=next(a for a in x['results'] if a['id']=='eth_depth_stress_80000')
        for label in z['future_only_labels']:
            self.assertTrue(all(v is None for v in label['hypothetical_mid_exit_cash_edge_bps_not_pnl'].values()))

    def test_policy_must_have_explicit_hypothetical_fee(self):
        x=copy.deepcopy(self.policy)
        x['exit_scenarios'][0]['exit_fee_bps']='NaN'
        with self.assertRaises(EvidenceReject):policy_checked(x)

    def test_policy_may_not_select_best(self):
        x=copy.deepcopy(self.policy)
        x['exit_scenarios'][0]['id']='highest_return'
        with self.assertRaises(EvidenceReject):policy_checked(x)

    def test_policy_unbounded_fee(self):
        x=copy.deepcopy(self.policy)
        x['exit_scenarios'][0]['exit_fee_bps']='999'
        with self.assertRaises(EvidenceReject):policy_checked(x)

    def test_invalid_decimal_precision(self):
        with self.assertRaises(EvidenceReject):dec('1e100','bad')

    def test_json_duplicate_object_key_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bad.json';p.write_text('{"a":1,"a":2}')
            with self.assertRaises(EvidenceReject):load(p)

    def test_json_nan_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bad.json';p.write_text('{"a":NaN}')
            with self.assertRaises(EvidenceReject):load(p)

    def test_independent_auditor_rejects_changed_theoretical_cost(self):
        x=copy.deepcopy(self.report)
        x['results'][0]['future_only_labels'][0]['hypothetical_mid_exit_cash_edge_bps_not_pnl']['baseline_mid_exit']='123.4'
        with self.assertRaisesRegex(IndependentMismatch,'arithmetic diverged'):
            verify(ROOT,x)

    def test_independent_auditor_rejects_false_fill(self):
        x=copy.deepcopy(self.report)
        x['results'][0]['verified_real_maker_or_taker_fills']='1'
        with self.assertRaisesRegex(IndependentMismatch,'Real fills'):
            verify(ROOT,x)

    def test_independent_auditor_rejects_false_quote_seq(self):
        x=copy.deepcopy(self.report)
        x['results'][0]['first_eligible_quote_sequence']=999999
        with self.assertRaisesRegex(IndependentMismatch,'source sequence'):
            verify(ROOT,x)

    def test_both_sell_and_buy_have_distinct_cost_signs(self):
        x=self.reproduce()
        buys=[r for r in x['results'] if r['side']=='BUY']
        sells=[r for r in x['results'] if r['side']=='SELL']
        self.assertTrue(buys and sells)
        self.assertTrue(all(r['hypothetical_sell_requires_independent_inventory_or_short_eligibility'] for r in sells))
        self.assertTrue(all(not r['hypothetical_sell_requires_independent_inventory_or_short_eligibility'] for r in buys))

    def test_evidence_lock_rejects_original_report_mutation(self):
        # Avoid a 600 MB duplicate source. Test lock byte-level binding explicitly.
        lock=load(ROOT/'examples/v22_frozen_evidence_lock.json')
        for name,expected in lock['files'].items():
            self.assertEqual(sha256((ROOT/name).read_bytes()).hexdigest(),expected)
        self.assertNotEqual(sha256((ROOT/'P1_POST_QUOTE_MARKOUT_v18.json').read_bytes()+b' ').hexdigest(),
                            lock['files']['P1_POST_QUOTE_MARKOUT_v18.json'])

if __name__=='__main__':unittest.main()
