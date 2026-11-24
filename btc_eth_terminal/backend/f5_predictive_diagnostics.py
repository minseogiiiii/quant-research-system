"""Descriptive information coefficients; no model selection or OOS claim."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import re
from decimal import Decimal, localcontext
from pathlib import Path
from statistics import fmean
from f4_ofi_markout import build
from f4_ofi_independent import audit
from f4_order_flow import read_packets
from independent_soak_replay import strict_json

FEATURES=("ofi_base","imbalance","microprice_edge_bps")
MIN_N=20

def need(ok,reason):
    if not ok:
        raise ValueError("F5_"+reason)

def dec(v):
    need(isinstance(v,str) and 0<len(v)<=160 and re.fullmatch(r"-?\d+(?:\.\d+)?",v),"DECIMAL")
    x=Decimal(v)
    need(x.is_finite() and abs(x)<=Decimal("1e30"),"DOMAIN")
    return x

def ranks(xs):
    result=[0.0]*len(xs)
    order=sorted(range(len(xs)),key=xs.__getitem__)
    i=0
    while i<len(order):
        j=i+1
        while j<len(order) and xs[order[i]]==xs[order[j]]:
            j+=1
        for k in order[i:j]:
            result[k]=(i+j+1)/2
        i=j
    return result

def corr(xs,ys):
    if len(xs)<MIN_N:
        return None
    a,b=fmean(xs),fmean(ys)
    dx=[x-a for x in xs];dy=[y-b for y in ys]
    scale=math.sqrt(math.fsum(x*x for x in dx)*math.fsum(y*y for y in dy))
    if scale==0:
        return None
    return max(-1.0,min(1.0,math.fsum(x*y for x,y in zip(dx,dy))/scale))

def metric(rows,field):
    x=[r[field] for r in rows]; y=[r["y"] for r in rows]
    xv=list(map(float,x));yv=list(map(float,y))
    groups={}
    for name,predicate in (("negative",lambda z:z<0),("zero",lambda z:z==0),("positive",lambda z:z>0)):
        a=[float(v) for u,v in zip(x,y) if predicate(u)]
        groups[name]={"n":len(a),"mean_markout_bps":fmean(a) if a else None}
    directional=[(a,b) for a,b in zip(x,y) if a!=0 and b!=0]
    return {"n":len(rows),"pearson_ic":corr(xv,yv),"rank_ic":corr(ranks(x),ranks(y)),
            "nonzero_direction_pairs":len(directional),
            "direction_agreement":sum((a>0)==(b>0) for a,b in directional)/len(directional) if len(directional)>=MIN_N else None,
            "conditional_markout":groups}

def summarize(report):
    need(report["schema"]=="f4_causal_ofi_markout_v1" and report["mode"]=="ARCHIVE_REPLAY"
         and all(report[k] is False for k in ("live_feed","execution_authorized","zero_gap_certified","oos_approved","alpha_confirmed")),"MODE")
    pin=report["source_manifest_sha256"]
    need(isinstance(pin,str) and re.fullmatch(r"[0-9a-f]{64}",pin) and report["epoch"]==pin+":0","EPOCH")
    p=report["policy"];start,end,step,window=p["start_ms"],p["end_ms"],p["step_ms"],p["window_ms"]
    horizons=p["horizons_ms"]
    need(all(type(v) is int for v in (start,end,step,window)) and 0<start<end<2**53
         and 100<=step<=60000 and start%step==end%step==0 and (end-start)//step<=2000
         and 100<=window<=300000 and isinstance(horizons,list) and 1<=len(horizons)<=6
         and all(type(h) is int and 100<=h<=60000 for h in horizons) and horizons==sorted(set(horizons)),"POLICY")
    need([f["decision_ms"] for f in report["features"]]==list(range(start,end,step)),"GRID")
    fdict={}
    with localcontext() as ctx:
        ctx.prec=80
        for f in report["features"]:
            t=f["decision_ms"]
            if f["status"]!="CAUSAL_FEATURE":
                need(f["status"] in ("NO_FRESH_QUOTE","INSUFFICIENT_OBSERVED_WARMUP","OUTSIDE_OBSERVED_SOURCE") and f["feature_available_ms"] is None,"MISSING_FEATURE")
                continue
            need(type(f["feature_available_ms"]) is int and 0<f["feature_available_ms"]<t
                 and t-f["feature_available_ms"]<=p["max_age_ms"],"FEATURE_TIME")
            mid=dec(f["mid"]);micro=dec(f["microprice"]);imb=dec(f["imbalance"])
            need(mid>0 and micro>0 and -1<=imb<=1,"FEATURE_DOMAIN")
            fdict[t]={"ofi_base":dec(f["ofi_base"]),"imbalance":imb,
                      "microprice_edge_bps":(micro-mid)*10000/mid}
    groups={h:[] for h in horizons};seen=set()
    for l in report["future_labels"]:
        t,h=l["decision_ms"],l["horizon_ms"]
        need(t in fdict and h in groups and (t,h) not in seen and l["target_ms"]==t+h,"LABEL_KEY")
        seen.add((t,h))
        if l["status"]!="OBSERVED_FUTURE_LABEL":
            need(l["status"] in ("NO_FUTURE_OBSERVATION","FIRST_L2_OUTSIDE_TOLERANCE")
                 and l["label_available_ms"] is None and l["midquote_return_bps"] is None,"MISSING_LABEL")
            continue
        a=l["label_available_ms"]
        need(type(a) is int and t+h<=a<=t+h+p["tolerance_ms"] and a<=report["source_last_receiver_ms"],"LABEL_TIME")
        groups[h].append({"t":t,"a":a,"y":dec(l["midquote_return_bps"]),**fdict[t]})
    need(seen=={(t,h) for t in fdict for h in horizons},"LABEL_INVENTORY")
    results=[]
    for h in horizons:
        rows=sorted(groups[h],key=lambda r:r["t"])
        separated=[];last_end=-1
        # Fixed greedy earliest decision; disjoint [t-window, label availability]
        # intervals. This is not an independence proof or a p-value.
        for r in rows:
            if r["t"]-window>last_end:
                separated.append(r);last_end=r["a"]
        results.append({"horizon_ms":h,"decision_grid_count":len(report["features"]),
                        "causal_features":len(fdict),"paired_labels":len(rows),"missing_labels":len(fdict)-len(rows),
                        "disjoint_pairs":len(separated),"features":[{"feature":k,"all_pairs":metric(rows,k),
                            "disjoint_intervals":metric(separated,k)} for k in FEATURES]})
    return {"schema":"f5_predictive_diagnostic_v1","status":"DESCRIPTIVE_ONLY",
            "source_manifest_sha256":pin,"epoch":report["epoch"],"product":report["product"],
            "policy":p,"minimum_ic_pairs":MIN_N,"results":results,
            "source_report_sha256":hashlib.sha256(json.dumps(report,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest(),
            "disjoint_policy":"GREEDY_EARLIEST_NONOVERLAPPING_FEATURE_TO_LABEL_INTERVAL",
            "p_values_reported":False,"independence_claim":False,"model_selected":False,
            "oos_approved":False,"alpha_confirmed":False,"execution_authorized":False}

def build_diagnostics(root,pin,**kwargs):
    report=build(root,pin,**kwargs)
    m=strict_json((Path(root)/"manifest.json").read_bytes())
    receipt=audit(read_packets(Path(root),m),report)
    out=summarize(report)
    out["independent_source_witness"]=receipt
    return out

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("archive",type=Path)
    p.add_argument("--manifest-sha256",required=True)
    p.add_argument("--product",choices=["BTC-USD","ETH-USD"],required=True)
    for k in ("start-ms","end-ms","step-ms","window-ms","max-age-ms","tolerance-ms"):
        p.add_argument("--"+k,type=int,required=True)
    p.add_argument("--horizons-ms",nargs="+",type=int,required=True)
    p.add_argument("--output",type=Path,required=True)
    a=vars(p.parse_args());root,pin,out=(a.pop(k) for k in ("archive","manifest_sha256","output"))
    result=build_diagnostics(root,pin,**a)
    with out.open("x") as f:
        json.dump(result,f,sort_keys=True,allow_nan=False)
        f.write("\n")

if __name__=="__main__":
    main()
