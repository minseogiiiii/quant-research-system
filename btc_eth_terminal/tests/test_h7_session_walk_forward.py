import hashlib
import tempfile
import unittest
from pathlib import Path
from h2_native_benchmark import generate,BASE
from h1_streaming_parquet import convert
from h7_session_walk_forward import run_folds,choose
class Forward(unittest.TestCase):
    def test_actual_epoch_replays_abstain_and_no_holdout(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);entries=[]
            for i in range(3):
                pin,*_=generate(root/f"raw{i}",2000000,levels=8,start_ms=BASE+i*60000)
                dest=root/f"dataset{i}";convert(root/f"raw{i}",pin,dest,fixture=True)
                entries.append(dict(dataset_path=str(dest),dataset_receipt_sha256=hashlib.sha256((dest/"dataset.json").read_bytes()).hexdigest(),role="DEVELOPMENT"))
            trials=[{"product":"BTC-USD","hold_ms":5000},{"product":"BTC-USD","hold_ms":10000}]
            r=run_folds(entries,trials,2,5000,42,root/"wf",fixture=True)
            self.assertEqual(r["folds"][0]["train_indices"],[0,1])
            self.assertEqual(r["folds"][0]["test_index"],2)
            self.assertEqual(r["folds"][0]["selection_status"],"NO_POSITIVE_ACTIVE_TRAINING_CANDIDATE")
            self.assertEqual(r["diagnostic_test_count"],0)
            self.assertFalse(r["oos_approved"])
            self.assertTrue((root/"wf"/"fold_000.db").exists())
            with self.assertRaises(ValueError):run_folds([*entries[:2],dict(entries[2],role="DECLARED_HOLDOUT")],trials,2,0,42,root/"bad",fixture=True)
    def test_selection_rule_and_incomplete_inventory(self):
        a={"trial_id":"a","status":"COMPLETE","mean_session_net_return":0.01,"sessions":[{"closed_roundtrips":1}]}
        b={"trial_id":"b","status":"COMPLETE","mean_session_net_return":0.03,"sessions":[{"closed_roundtrips":1}]}
        self.assertEqual(choose({"all_complete":True,"results":[a,b]})[0],"b")
        self.assertIsNone(choose({"all_complete":False,"results":[a,b]})[0])
        self.assertIsNone(choose({"all_complete":True,"results":[dict(a,mean_session_net_return=0)]})[0])
