"""Local causal H3 feature cache; never a cache of execution or future labels."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import tempfile
from decimal import Decimal,localcontext
from pathlib import Path
from h3_momentum_horizons import POLICY,project,PRODUCTS
from h9_portfolio import simulate
from f4_depth_buckets import units,SCALE
from h1_f6_dataset_replay import replay_dataset
from h5_experiment_registry import sha,hex64
from independent_soak_replay import strict_json

ROOT=Path(__file__).resolve().parents[1]
FEATURE_KEYS=("grid_ms","momentum_ms","ofi_ms","max_age_ms")
SOURCES=("backend/h3_momentum_horizons.py","backend/f4_ofi_markout.py","backend/f4_depth_buckets.py",
         "backend/post_quote_markout.py","backend/f4_order_flow.py","backend/f5_independent_clock.py",
         "backend/independent_soak_replay.py","scripts/h1_f6_dataset_replay.py","scripts/h10_feature_cache.py")
MAX_ROWS=200000
MAX_LINE=2048
def need(ok,why):
    if not ok:raise ValueError("H10_"+why)
def canonical(x):return json.dumps(x,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
def code_pin():
    return sha({name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCES})
def binding(source,receipt,p,fixture):
    need(hex64(source) and hex64(receipt) and type(p) is dict and set(p)==set(POLICY),"PINS_POLICY")
    f={k:p[k] for k in FEATURE_KEYS}
    grid,look,window,age=(f[k] for k in FEATURE_KEYS)
    need(all(type(x) is int for x in f.values()) and 100<=grid<=60000 and grid<=look<=900000
         and 100<=window<=60000 and 0<=age<=5000,"FEATURE_CLOCK")
    return {"schema":"h10_feature_cache_key_v1","source_manifest_sha256":source,
            "dataset_receipt_sha256":receipt,"feature_code_sha256":code_pin(),"feature_policy":f,
            "source_kind":"SYNTHETIC_TEST_ONLY" if fixture else "ARCHIVE_REPLAY"}
def bounded(path,max_bytes):
    need(path.is_file() and not path.is_symlink() and 0<path.stat().st_size<=max_bytes,"FILE")
    with path.open("rb") as f:raw=f.read(max_bytes+1)
    need(len(raw)<=max_bytes,"FILE_SIZE");return raw

def build(dataset,source,receipt,dest,policy=None,*,fixture=False):
    p=POLICY if policy is None else policy;dest=Path(dest)
    need(not dest.exists() and not dest.is_symlink(),"OUTPUT")
    key=binding(source,receipt,p,fixture);frozen=dict(POLICY,**key["feature_policy"])
    with tempfile.TemporaryDirectory(prefix=".h10-",dir=dest.parent) as d:
        stage=Path(d)/"cache";stage.mkdir();tables={}
        for product in PRODUCTS:
            path=stage/(product+".jsonl");h=hashlib.sha256();n=valid=0
            with path.open("xb") as f:
                def emit(row):
                    nonlocal n,valid
                    raw=canonical(row)+b"\n";n+=1;valid+=int(row[1] is not None)
                    need(n<=MAX_ROWS and len(raw)<=MAX_LINE,"ROW_BUDGET")
                    f.write(raw);h.update(raw)
                result=replay_dataset(dataset,source,product,fixture=fixture,receipt_pin=receipt,
                    projector=lambda packets,prod:project(packets,prod,frozen,feature_sink=emit,features_only=True))
            need(n==result["decisions"] and valid==result["eligible_decisions"] and n-valid==result["missing_decisions"],"PROJECTION_COUNT")
            tables[product]={"projection":result,"file_sha256":h.hexdigest(),"bytes":path.stat().st_size}
        report={"schema":"h10_feature_cache_v1","key":key,"key_sha256":sha(key),"tables":tables,
                "future_labels_present":False,"execution_assumptions_cached":False,"oos_approved":False,
                "alpha_confirmed":False,"execution_authorized":False}
        raw=canonical(report)+b"\n";(stage/"cache.json").write_bytes(raw)
        need(not dest.exists(),"OUTPUT_RACE");os.rename(stage,dest)
    return report,hashlib.sha256(raw).hexdigest()

def load(cache,cache_pin,source,receipt,policy=None,*,fixture=False):
    p=POLICY if policy is None else policy;root=Path(cache)
    need(root.is_dir() and not root.is_symlink() and hex64(cache_pin),"CACHE_PATH")
    raw=bounded(root/"cache.json",1000000)
    need(hashlib.sha256(raw).hexdigest()==cache_pin,"CACHE_PIN")
    r=strict_json(raw);key=binding(source,receipt,p,fixture)
    need(r["schema"]=="h10_feature_cache_v1" and r["key"]==key and r["key_sha256"]==sha(key)
         and set(r["tables"])==set(PRODUCTS)
         and all(r[k] is False for k in ("future_labels_present","execution_assumptions_cached","oos_approved","alpha_confirmed","execution_authorized")),"CACHE_CONTRACT")
    grid,look,window,age=(key["feature_policy"][k] for k in FEATURE_KEYS)
    threshold,imbalance=Decimal(p["momentum_bps"]),Decimal(p["imbalance_min"])
    need(threshold.is_finite() and 0<=threshold<=10000 and imbalance.is_finite() and 0<=imbalance<=1,"THRESHOLD")
    signals=[];coverage={};source_span=None
    with localcontext() as ctx:
        ctx.prec=80
        for product in PRODUCTS:
            meta=r["tables"][product];proj=meta["projection"]
            n=proj["source_messages"];first=proj["source_first_ms"];last=proj["source_last_ms"]
            need(proj["schema"]=="h10_feature_projection_v1" and proj["product"]==product and type(n) is int and n>0
                 and type(first) is int and type(last) is int and 0<first<=last<2**53,"SOURCE_SPAN")
            span=(n,first,last)
            need(source_span is None or source_span==span,"PRODUCT_SOURCE_PARITY");source_span=span
            t0=((first+grid-1)//grid)*grid;expected=max(0,(last-t0)//grid+1)
            need(expected<=MAX_ROWS and proj["decisions"]==expected,"GRID_COUNT")
            path=root/(product+".jsonl")
            need(path.is_file() and not path.is_symlink() and path.stat().st_size==meta["bytes"]<=MAX_ROWS*MAX_LINE
                 and hex64(meta["file_sha256"]),"TABLE")
            count=valid=byte_count=0;prev_seq=-1;h=hashlib.sha256()
            with path.open("rb") as f:
                while line:=f.readline(MAX_LINE+1):
                    need(len(line)<=MAX_LINE and line.endswith(b"\n") and count<expected,"LINE")
                    h.update(line);byte_count+=len(line);row=strict_json(line)
                    need(type(row) is list and len(row) in (2,10) and type(row[0]) is int and row[0]==t0+count*grid
                         and line==canonical(row)+b"\n","ROW")
                    count+=1;t=row[0]
                    if len(row)==2:
                        need(row[1] is None,"MISSING_ROW");continue
                    need(all(type(v) is int and abs(v)<10**80 for v in row),"INTEGER")
                    _,stamp,seq,anchor,oldseq,mid,oldmid,flow,bq,aq=row
                    need(first<=anchor<t-look and t-look-anchor<=age and anchor<=stamp<t and t-stamp<=age
                         and 0<=oldseq<=seq<n and seq>=prev_seq and mid>0 and oldmid>0 and bq>0 and aq>0,"CAUSAL_ROW")
                    prev_seq=seq;valid+=1
                    mom=Decimal((mid-oldmid)*10000)/Decimal(oldmid)
                    if mom>=threshold:signals.append((t,product,seq,flow>0 and Decimal(bq-aq)/Decimal(bq+aq)>=imbalance))
            need(count==expected and valid==proj["eligible_decisions"] and count-valid==proj["missing_decisions"]
                 and byte_count==meta["bytes"] and h.hexdigest()==meta["file_sha256"],"TABLE_PARITY")
            coverage[product]={"decisions":count,"eligible":valid,"missing":count-valid}
    need((root/"cache.json").read_bytes()==raw,"RECEIPT_CHANGED")
    return sorted(signals),coverage,source_span

def run_cached(dataset,cache,cache_pin,source,receipt,policy=None,*,initial_cash="10000",fixture=False):
    p=POLICY if policy is None else policy
    sig,coverage,span=load(cache,cache_pin,source,receipt,p,fixture=fixture)
    lat,tol=p['latency_ms'],p['tolerance_ms'];hs=p['horizons_ms']
    need(type(lat) is int and 1<=lat<=5000 and type(tol) is int and 0<=tol<=10000
         and type(hs) is list and 1<=len(hs)<=6 and all(type(h) is int and 100<=h<=900000 for h in hs)
         and hs==sorted(set(hs)), 'EXECUTION_CLOCK')
    need(type(p['quantity']) is dict and set(p['quantity'])==set(PRODUCTS)
         and all(0<units(q)<=1000000*SCALE for q in p['quantity'].values()),'QUANTITY')
    cap,fee,slip=map(units,(p['capacity_fraction'],p['fee_bps'],p['extra_slippage_bps']))
    need(0<cap<=SCALE and 0<=fee<=100*SCALE and 0<=slip<=500*SCALE,'EXECUTION_COST')
    result=replay_dataset(dataset,source,"BTC-USD",fixture=fixture,receipt_pin=receipt,
        projector=lambda packets,product:simulate(packets,sig,p,initial_cash=initial_cash))
    need((result["source_messages"],result["source_first_ms"],result["source_last_ms"])==span,"REPLAY_SPAN")
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("dataset",type=Path);p.add_argument("cache",type=Path)
    p.add_argument("--source-sha256",required=True);p.add_argument("--dataset-receipt-sha256",required=True)
    p.add_argument("--policy",type=Path);sub=p.add_subparsers(dest="action",required=True)
    sub.add_parser("build");q=sub.add_parser("replay");q.add_argument("--cache-sha256",required=True)
    q.add_argument("--initial-cash",default="10000");q.add_argument("--output",type=Path,required=True)
    a=p.parse_args();policy=strict_json(bounded(a.policy,100000)) if a.policy else POLICY
    if a.action=="build":
        r,pin=build(a.dataset,a.source_sha256,a.dataset_receipt_sha256,a.cache,policy)
        print(json.dumps({"cache_sha256":pin,"key_sha256":r["key_sha256"]}))
    else:
        r=run_cached(a.dataset,a.cache,a.cache_sha256,a.source_sha256,a.dataset_receipt_sha256,policy,initial_cash=a.initial_cash)
        with a.output.open("x") as f:json.dump(r,f,sort_keys=True,allow_nan=False);f.write("\n")
if __name__=="__main__":main()
