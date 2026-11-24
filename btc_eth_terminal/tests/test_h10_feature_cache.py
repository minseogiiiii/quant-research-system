import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from h2_native_benchmark import generate
from h1_streaming_parquet import convert
from h3_momentum_horizons import POLICY,project
from h9_portfolio import collect,simulate
from h10_feature_cache import build,load,run_cached
from f5_independent_clock import read_independent
from independent_soak_replay import strict_json

class Cache(unittest.TestCase):
    def test_reuse_and_invalidation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);raw=root/"raw";pin,*_=generate(raw,2_000_000,levels=8)
            data=root/"data";convert(raw,pin,data,fixture=True)
            receipt=hashlib.sha256((data/"dataset.json").read_bytes()).hexdigest()
            p=copy.deepcopy(POLICY);p.update(grid_ms=100,momentum_ms=200,ofi_ms=200,
              momentum_bps="0",horizons_ms=[100,300],max_age_ms=1000)
            cache=root/"cache";r,cp=build(data,pin,receipt,cache,p,fixture=True)
            m=strict_json((raw/"manifest.json").read_bytes());factory=lambda:read_independent(raw,m)
            for policy in (p,dict(p,momentum_bps="2",fee_bps="1",horizons_ms=[100,900])):
                direct,_=collect(factory,policy)
                sig,coverage,_=load(cache,cp,pin,receipt,policy,fixture=True)
                self.assertEqual(sig,direct)
                self.assertEqual(run_cached(data,cache,cp,pin,receipt,policy,fixture=True),simulate(factory(),direct,policy))
            for source,dp,policy,mode in [(pin,receipt,dict(p,ofi_ms=300),True),("0"*64,receipt,p,True),
                                           (pin,"0"*64,p,True),(pin,receipt,p,False)]:
                with self.assertRaises(ValueError):load(cache,cp,source,dp,policy,fixture=mode)
            with self.assertRaises(ValueError):load(cache,"0"*64,pin,receipt,p,fixture=True)
            f=cache/"BTC-USD.jsonl";f.write_bytes(f.read_bytes()+b"[]\n")
            with self.assertRaises(ValueError):load(cache,cp,pin,receipt,p,fixture=True)
    def test_feature_only_does_not_call_walk(self):
        from unittest.mock import patch
        from test_h3_momentum_horizons import Horizon
        rows,p=Horizon().setup();features=[]
        with patch("h3_momentum_horizons.walk",side_effect=AssertionError("unexpected execution")):
            r=project(rows,"BTC-USD",p,features_only=True,feature_sink=features.append)
        self.assertEqual(r["decisions"],len(features))
        self.assertGreater(r["eligible_decisions"],0)
        self.assertTrue(any(len(x)==2 for x in features))
