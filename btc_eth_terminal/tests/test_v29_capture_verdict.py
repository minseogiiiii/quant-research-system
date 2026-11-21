"""v29 terminal admission must fail closed, using synthetic-only contract data."""
from __future__ import annotations
import json
from copy import deepcopy
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'backend'))
sys.path.insert(0, str(ROOT/'scripts'))
from v29_capture_verdict import evaluate


class V29TerminalVerdict(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root/'work'
        self.work.mkdir()
        self.evidence = self.root/'observer'
        self.evidence.mkdir()
        self.source = self.work/'soak_evidence'/'coinbase_btc_eth_soak_TEST'
        self.source.mkdir(parents=True)
        self.mode = '24h'
        self.hash = 'a'*64
        self.preflight = {'schema':'v25_host_preflight_v1','mode':'real_public_24h',
                          'ready_for_real_capture':True,'failures':[],
                          'observation':{'boot_id_digest':'b'*64,'storage_root':str(self.work),
                                         'observed_utc':'2026-11-15T16:00:00Z','monotonic_ns':100000000000}}
        self.operator = {'schema':'v25_operator_evidence_v1','run_mode':'real_public_24h',
                         'capture_exit_code':0,'capture_failure':None,'injection_options_disabled':True,
                         'orders_enabled':False,'before':{'boot_id_digest':'b'*64,'storage_root':str(self.work),
                         'observed_utc':'2026-11-15T16:00:02Z','monotonic_ns':102000000000},
                         'source_archive_dir':str(self.source), 'archive_manifest_sha256':self.hash}
        self.ready_window = {'schema':'v29_source_ready_window_v1',
                             'source_window_contract_satisfied':True,'mode':'24h',
                             'required_ready_window_seconds':86400,
                             'observed_ready_window_seconds':86402.,
                             'host_reported_receiver_clock_not_exchange_attested':True}
        self.crosscheck = {'schema':'v26_capture_crosscheck_v1',
                           'status':'HOST_MONITORED_INTERNAL_SOURCE_CANDIDATE',
                           'candidate_host_monitored_source':True,'source_manifest_sha256':self.hash,
                           'prior_internal_admission':{'status':'HOST_REPORTED_24H_AND_DUAL_REPLAY_CANDIDATE',
                              'dual_replay_book_digests_match':True,'source_manifest_sha256':self.hash},
                           'host_clock_trace':{'samples':289,'host_reported_only':True,
                                'independent_exchange_time_attested':False},
                           'independent_exchange_truth_verified':False,
                           'historical_feature_availability_verified':False,
                           'executable_alpha_verified':False,'real_orders_sent':0}

    def evaluate(self,**updates):
        data={'preflight':self.preflight,'operator':self.operator,'crosscheck':self.crosscheck,
              'ready_window':self.ready_window,
              'mode':self.mode,'workdir':self.work,'evidence_dir':self.evidence,
              'operator_exit_code':0,'execution_requested':True}
        data.update(updates)
        return evaluate(**data)

    def test_synthetic_contract_candidate(self):
        result=self.evaluate()
        self.assertTrue(result['candidate_host_monitored_internal_source'])
        self.assertEqual(result['status'],'HOST_MONITORED_INTERNAL_24H_CANDIDATE')
        self.assertFalse(result['independent_exchange_time_attested'])
        self.assertFalse(result['independent_blind_oos_eligible'])
    def test_never_executed_is_not_admitted(self):
        self.assertIn('CAPTURE_NOT_EXECUTED',self.evaluate(execution_requested=False)['problems'])
    def test_blocked_preflight_is_not_admitted(self):
        bad=deepcopy(self.preflight);bad['ready_for_real_capture']=False;bad['failures']=['DNS']
        self.assertIn('PREFLIGHT_NOT_READY',self.evaluate(preflight=bad)['problems'])
    def test_bad_preflight_schema_or_mode(self):
        bad=deepcopy(self.preflight);bad['mode']='real_public_smoke_60s'
        self.assertIn('PREFLIGHT_MODE_OR_SCHEMA_MISMATCH',self.evaluate(preflight=bad)['problems'])
    def test_nonzero_operator_exit(self):
        self.assertIn('CAPTURE_OPERATOR_NONZERO_EXIT',self.evaluate(operator_exit_code=2)['problems'])
    def test_operator_nonzero_capture_exit(self):
        bad=deepcopy(self.operator);bad['capture_exit_code']=2
        self.assertIn('SOURCE_CAPTURE_FAILED',self.evaluate(operator=bad)['problems'])
    def test_operator_capture_failure(self):
        bad=deepcopy(self.operator);bad['capture_failure']='CAPTURE_RUNTIME_LIMIT_EXCEEDED'
        self.assertIn('SOURCE_CAPTURE_FAILED',self.evaluate(operator=bad)['problems'])
    def test_injection_flag(self):
        bad=deepcopy(self.operator);bad['injection_options_disabled']=False
        self.assertIn('NO_ORDER_OR_INJECTION_CONTRACT_FAILED',self.evaluate(operator=bad)['problems'])
    def test_orders_true_rejected(self):
        bad=deepcopy(self.operator);bad['orders_enabled']=True
        self.assertIn('NO_ORDER_OR_INJECTION_CONTRACT_FAILED',self.evaluate(operator=bad)['problems'])
    def test_preflight_boot_switch(self):
        bad=deepcopy(self.preflight);bad['observation']['boot_id_digest']='c'*64
        self.assertIn('PREFLIGHT_BOOT_ID_UNBOUND',self.evaluate(preflight=bad)['problems'])
    def test_preflight_stale_even_with_same_boot(self):
        bad=deepcopy(self.preflight);bad['observation']['monotonic_ns']=10000000000
        self.assertIn('STALE_OR_REVERSED_PREFLIGHT_OBSERVATION',self.evaluate(preflight=bad)['problems'])
    def test_preflight_reversed_wall_time(self):
        bad=deepcopy(self.preflight);bad['observation']['observed_utc']='2026-11-15T16:00:05Z'
        self.assertIn('STALE_OR_REVERSED_PREFLIGHT_OBSERVATION',self.evaluate(preflight=bad)['problems'])
    def test_missing_preflight_monotonic(self):
        bad=deepcopy(self.preflight);bad['observation'].pop('monotonic_ns')
        self.assertIn('INVALID_PREFLIGHT_OBSERVATION_TIMING',self.evaluate(preflight=bad)['problems'])
    def test_preflight_storage_switch(self):
        bad=deepcopy(self.preflight);bad['observation']['storage_root']='/tmp/other'
        self.assertIn('PREFLIGHT_STORAGE_ROOT_UNBOUND',self.evaluate(preflight=bad)['problems'])
    def test_crosscheck_not_candidate(self):
        bad=deepcopy(self.crosscheck);bad['candidate_host_monitored_source']=False
        self.assertIn('V26_FULL_DUAL_REPLAY_AND_CLOCK_CROSSCHECK_NOT_ADMITTED',self.evaluate(crosscheck=bad)['problems'])
    def test_wrong_duration_source_label(self):
        bad=deepcopy(self.crosscheck);bad['prior_internal_admission']['status']='HOST_REPORTED_60S_AND_DUAL_REPLAY_CANDIDATE'
        self.assertIn('WRONG_DURATION_OR_NO_INDEPENDENT_SOURCE_REPLAY',self.evaluate(crosscheck=bad)['problems'])
    def test_weak_clock_trace(self):
        bad=deepcopy(self.crosscheck);bad['host_clock_trace']['samples']=249
        self.assertIn('INCOMPLETE_OR_OVERCLAIMED_CLOCK_TRACE',self.evaluate(crosscheck=bad)['problems'])
    def test_missing_valid_ready_window(self):
        self.assertIn('ACTUAL_BOTH_BOOKS_READY_WINDOW_NOT_PROVEN',self.evaluate(ready_window=None)['problems'])
    def test_process_duration_cannot_substitute_for_ready_window(self):
        bad=deepcopy(self.ready_window);bad['observed_ready_window_seconds']=86399.999
        self.assertIn('ACTUAL_BOTH_BOOKS_READY_WINDOW_NOT_PROVEN',self.evaluate(ready_window=bad)['problems'])
    def test_misclassified_short_smoke_as_full_day(self):
        bad=deepcopy(self.ready_window);bad['mode']='smoke'
        self.assertIn('ACTUAL_BOTH_BOOKS_READY_WINDOW_NOT_PROVEN',self.evaluate(ready_window=bad)['problems'])
    def test_falsely_attested_clock(self):
        bad=deepcopy(self.crosscheck);bad['host_clock_trace']['independent_exchange_time_attested']=True
        self.assertIn('INCOMPLETE_OR_OVERCLAIMED_CLOCK_TRACE',self.evaluate(crosscheck=bad)['problems'])
    def test_manifest_hash_mismatch(self):
        bad=deepcopy(self.crosscheck);bad['source_manifest_sha256']='c'*64
        self.assertIn('MANIFEST_SOURCE_HASH_NOT_BOUND',self.evaluate(crosscheck=bad)['problems'])
    def test_claimed_alpha_rejected(self):
        bad=deepcopy(self.crosscheck);bad['executable_alpha_verified']=True
        self.assertIn('UNSUPPORTED_MARKET_TRUTH_ALPHA_OR_ORDER_CLAIM',self.evaluate(crosscheck=bad)['problems'])
    def test_claimed_real_orders_rejected(self):
        bad=deepcopy(self.crosscheck);bad['real_orders_sent']=1
        self.assertIn('UNSUPPORTED_MARKET_TRUTH_ALPHA_OR_ORDER_CLAIM',self.evaluate(crosscheck=bad)['problems'])
    def test_source_in_wrong_directory(self):
        bad=deepcopy(self.operator);bad['source_archive_dir']=str(self.evidence)
        self.assertIn('UNSAFE_OR_UNBOUND_SOURCE_DIRECTORY',self.evaluate(operator=bad)['problems'])
    def test_symlink_source_rejected(self):
        fake=self.work/'soak_evidence'/'coinbase_btc_eth_soak_LINK';fake.symlink_to(self.source,target_is_directory=True)
        bad=deepcopy(self.operator);bad['source_archive_dir']=str(fake)
        self.assertIn('UNSAFE_OR_UNBOUND_SOURCE_DIRECTORY',self.evaluate(operator=bad)['problems'])
    def test_smoke_must_be_smoke_source(self):
        bad_pre=deepcopy(self.preflight);bad_pre['mode']='real_public_smoke_60s'
        bad_op=deepcopy(self.operator);bad_op['run_mode']='real_public_smoke_60s'
        bad_co=deepcopy(self.crosscheck);bad_co['host_clock_trace']['samples']=4
        self.assertIn('WRONG_DURATION_OR_NO_INDEPENDENT_SOURCE_REPLAY',
                      self.evaluate(preflight=bad_pre,operator=bad_op,crosscheck=bad_co,mode='smoke')['problems'])
    def test_smoke_correct_source_classification(self):
        bad_pre=deepcopy(self.preflight);bad_pre['mode']='real_public_smoke_60s'
        bad_op=deepcopy(self.operator);bad_op['run_mode']='real_public_smoke_60s'
        bad_co=deepcopy(self.crosscheck);bad_co['host_clock_trace']['samples']=4
        bad_co['prior_internal_admission']['status']='HOST_REPORTED_60S_AND_DUAL_REPLAY_CANDIDATE'
        window=deepcopy(self.ready_window);window.update({'mode':'smoke','required_ready_window_seconds':60,'observed_ready_window_seconds':62.0})
        self.assertEqual(self.evaluate(preflight=bad_pre,operator=bad_op,crosscheck=bad_co,ready_window=window,mode='smoke')['status'],
                         'HOST_MONITORED_INTERNAL_60S_CANDIDATE')
    def test_no_crosscheck(self):
        self.assertIn('V26_FULL_DUAL_REPLAY_AND_CLOCK_CROSSCHECK_NOT_ADMITTED',self.evaluate(crosscheck=None)['problems'])
    def test_missing_archive(self):
        self.source.rmdir()
        self.assertIn('UNSAFE_OR_UNBOUND_SOURCE_DIRECTORY',self.evaluate()['problems'])
    def test_legacy_v25_cli_incomplete_source_exits_nonzero(self):
        from v25_capture_admission import main
        from unittest.mock import patch
        source=self.root/'fake_archive';source.mkdir()
        output=self.root/'audit.json'
        with patch('v25_capture_admission.assess_archive',return_value={
                'schema':'v25_capture_admission_v1',
                'status':'INTERNAL_REPLAY_ONLY_HOST_EVIDENCE_INCOMPLETE',
                'candidate_24h_internal_source':False,
                'candidate_real_60s_smoke':False}),\
             patch.object(sys,'argv',['v25_capture_admission.py',str(source),'--output',str(output)]):
            with self.assertRaises(SystemExit) as context:
                main()
        self.assertEqual(context.exception.code,2)
        self.assertFalse(json.loads(output.read_text())['candidate_24h_internal_source'])
    def test_legacy_v25_cli_rejected_source_exits_nonzero(self):
        from v25_capture_admission import main
        from unittest.mock import patch
        output=self.root/'audit.json'
        with patch('v25_capture_admission.assess_archive',side_effect=ValueError('BAD')),\
             patch.object(sys,'argv',['v25_capture_admission.py',str(self.source),'--output',str(output)]):
            with self.assertRaises(SystemExit) as context:
                main()
        self.assertEqual(context.exception.code,2)
        self.assertEqual(json.loads(output.read_text())['status'],'REJECTED_SOURCE')
    def test_existing_observer_is_not_modified(self):
        import subprocess
        old=self.root/'existing';old.mkdir();marker=old/'marker.txt';marker.write_text('immutable')
        p=subprocess.run([sys.executable,str(ROOT/'scripts/v29_verified_capture.py'),
                          '--workdir',str(self.work),'--evidence-dir',str(old),
                          '--mode','24h'],capture_output=True,text=True,timeout=10)
        self.assertEqual(p.returncode,2)
        self.assertEqual(marker.read_text(),'immutable')
        self.assertEqual(sorted(x.name for x in old.iterdir()),['marker.txt'])
    def test_observer_nested_under_work_is_never_created(self):
        import subprocess
        nested=self.work/'unsafe'
        p=subprocess.run([sys.executable,str(ROOT/'scripts/v29_verified_capture.py'),
                          '--workdir',str(self.work),'--evidence-dir',str(nested),
                          '--mode','24h'],capture_output=True,text=True,timeout=10)
        self.assertEqual(p.returncode,2)
        self.assertFalse(nested.exists())
    def test_real_local_preflight_no_market_connection_or_source(self):
        evidence=self.root/'observed';work=self.work
        p=subprocess.run([sys.executable,str(ROOT/'scripts/v29_verified_capture.py'),
                          '--workdir',str(work),'--evidence-dir',str(evidence),
                          '--mode','24h','--raw-budget','1048576',
                          '--archive-budget','2097152','--disk-reserve','0'],
                          capture_output=True,text=True,timeout=15)
        self.assertEqual(p.returncode,2,p.stdout+p.stderr)
        self.assertFalse((work/'soak_evidence'/'coinbase_btc_eth_soak_REAL').exists())
        result=json.loads((evidence/'V29_FINAL_VERDICT.json').read_text())
        self.assertEqual(result['status'],'BLOCKED_FINAL_CAPTURE_GATE')
        self.assertIn('CAPTURE_NOT_EXECUTED',result['problems'])
        self.assertIn('PREFLIGHT_NOT_READY',result['problems'])
        self.assertFalse((evidence/'V29_FULL_CROSSCHECK.json').exists())
    def test_real_local_execute_blocked_no_market_source(self):
        evidence=self.root/'observed';work=self.work
        p=subprocess.run([sys.executable,str(ROOT/'scripts/v29_verified_capture.py'),
                          '--workdir',str(work),'--evidence-dir',str(evidence),
                          '--mode','24h','--raw-budget','1048576',
                          '--archive-budget','2097152','--disk-reserve','0',
                          '--execute-real-public-capture'],
                          capture_output=True,text=True,timeout=15)
        self.assertEqual(p.returncode,2,p.stdout+p.stderr)
        self.assertFalse((work/'soak_evidence'/'coinbase_btc_eth_soak_REAL').exists())
        verdict=json.loads((evidence/'V29_FINAL_VERDICT.json').read_text())
        self.assertFalse(verdict['candidate_host_monitored_internal_source'])
        self.assertIn('PREFLIGHT_NOT_READY',verdict['problems'])
        self.assertFalse((evidence/'V29_FULL_CROSSCHECK.json').exists())
