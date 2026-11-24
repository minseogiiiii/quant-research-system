"""Real previously inspected source, cache equality and measured reuse."""
import argparse
import hashlib
import json
import shutil
import tempfile
import time
from pathlib import Path
from q13_private_fixture_gate import verify_zip,PREFIX
from independent_soak_replay import strict_json
from h1_streaming_parquet import convert
from h1_f6_dataset_replay import replay_dataset
from h3_momentum_horizons import project,POLICY,PRODUCTS
from h9_portfolio import simulate,collect
from h10_feature_cache import build,load,run_cached
from f5_independent_clock import read_independent
from f6_liquidity_recovery import digest
PIN="8f2e0ec88a601a92ab4c7bcd0e04b20536fbe46bbd8b3c7f67ae3c1d075c28e5"
SOURCE=PREFIX+"backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy/"
def require(ok,reason):
    if not ok:raise ValueError(reason)
def gate(archive,output):
    with tempfile.TemporaryDirectory(prefix="h10-real-") as d:
        root=Path(d)/"raw";root.mkdir()
        with verify_zip(archive) as z:
            manifest=z.read(SOURCE+"manifest.json")
            require(hashlib.sha256(manifest).hexdigest()==PIN,"F4_REAL_PIN")
            m=strict_json(manifest)
            require(m.get("synthetic_fixture") is not True,"F4_REAL_SYNTHETIC")
            (root/"manifest.json").write_bytes(manifest)
            (root/"chunks").mkdir()
            for c in m["chunks"]:
                import re
                name=c["file"]
                require(re.fullmatch(r"chunks/chunk_\d{5}_conn_\d{3}\.jsonl",name),"F4_REAL_PATH")
                info=z.getinfo(SOURCE+name)
                require(info.file_size==c["archived_line_bytes"] and info.file_size<=32000000,"F4_REAL_SIZE")
                with z.open(info) as src,(root/name).open("xb") as dst:
                    shutil.copyfileobj(src,dst)
        dest=Path(d)/"dataset";convert(root,PIN,dest)
        dp=hashlib.sha256((dest/"dataset.json").read_bytes()).hexdigest()
        cache=Path(d)/"cache"
        start=time.perf_counter();r,cp=build(dest,PIN,dp,cache);build_seconds=time.perf_counter()-start
        variants=[(POLICY,"10000"),(dict(POLICY,horizons_ms=[10000,60000],fee_bps="2"),"10000"),
                  (dict(POLICY,momentum_bps="3",imbalance_min="0.2"),"1000")]
        variants_out=[]
        for policy,cash in variants:
            start=time.perf_counter();signals=[]
            for prod in PRODUCTS:
                def projector(packets,product):return project(packets,product,policy,signal_sink=signals.append)
                replay_dataset(dest,PIN,prod,projector=projector,receipt_pin=dp)
            direct_signals=sorted(signals);direct_signal_seconds=time.perf_counter()-start
            start=time.perf_counter()
            direct=replay_dataset(dest,PIN,"BTC-USD",projector=lambda packets,product:simulate(packets,direct_signals,policy,initial_cash=cash),receipt_pin=dp)
            direct_execution_seconds=time.perf_counter()-start
            start=time.perf_counter()
            sig,coverage,span=load(cache,cp,PIN,dp,policy)
            load_seconds=time.perf_counter()-start
            require(sig==direct_signals,"H10_SIGNAL_PARITY")
            start=time.perf_counter();cached=run_cached(dest,cache,cp,PIN,dp,policy,initial_cash=cash)
            cached_seconds=time.perf_counter()-start
            require(direct==cached,"H10_PORTFOLIO_PARITY")
            native,_=collect(lambda:read_independent(root,m),policy)
            require(native==sig,"H10_NATIVE_PARITY")
            variants_out.append({"policy_sha256":digest(policy),"initial_cash":cash,"signals":len(sig),
                "portfolio_scenarios":len(cached["portfolios"]),"portfolio_sha256":digest(cached),
                "direct_signal_seconds":direct_signal_seconds,"direct_execution_seconds":direct_execution_seconds,
                "cache_signal_seconds":load_seconds,"cached_replay_seconds":cached_seconds,
                "signal_and_portfolio_parity":True})
        result={"schema":"h10_real_cache_receipt_v1","status":"PASS","source_manifest_sha256":PIN,
                "source_messages":m["messages"],"source_kind":"PREVIOUSLY_INSPECTED_HISTORICAL",
                "cache_key_sha256":r["key_sha256"],"cache_receipt_sha256":cp,
                "cache_bytes":sum(f.stat().st_size for f in cache.iterdir()),"build_seconds":build_seconds,
                "coverage":coverage,"variants":variants_out,"raw_data_uploaded":False,
                "oos_approved":False,"alpha_confirmed":False,"execution_authorized":False}
        with output.open("x") as f:json.dump(result,f,sort_keys=True,allow_nan=False);f.write("\n")
        print("H10_REAL_CACHE_REUSE_PASS")
if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--release-zip",type=Path,required=True);p.add_argument("--output",type=Path,required=True)
    a=p.parse_args();gate(a.release_zip,a.output)
