"""Native-only, local catalog of externally pinned H1 dataset receipts.

Partitions whole observed epochs by predeclared role. Does not attest that a
declared holdout is unseen, statistically independent, or approved for OOS.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
from pathlib import Path
import duckdb
from independent_soak_replay import strict_json
from h1_streaming_parquet import digest
from q293_epoch_audit import identity

ROLES={"DEVELOPMENT":0,"VALIDATION":1,"DECLARED_HOLDOUT":2}
TABLES=("packets","l2","trades")
MAX_SESSIONS=1024
def need(ok,why):
    if not ok:raise ValueError("H4_"+why)
def hex64(x):return isinstance(x,str) and re.fullmatch(r"[0-9a-f]{64}",x) is not None
def canonical(x):return json.dumps(x,sort_keys=True,separators=(",",":"),allow_nan=False).encode()

def inspect(root,pin,role,fixture):
    root=Path(root);need(root.is_dir() and not root.is_symlink(),"DATASET_PATH")
    need(hex64(pin) and role in ROLES,"PIN_ROLE")
    receipt=root/"dataset.json";before=identity(receipt)
    need(0<before[2]<=1000000,"RECEIPT_SIZE")
    raw=receipt.read_bytes()
    need(hashlib.sha256(raw).hexdigest()==pin and identity(receipt)==before,"RECEIPT_PIN")
    r=strict_json(raw)
    need(r["schema"]=="h1_historical_dataset_v1" and hex64(r["source_manifest_sha256"])
         and r["epoch"]==r["source_manifest_sha256"]+":0" and r["independent_replay_parity"] is True
         and all(r[k] is False for k in ("live_feed","oos_approved","alpha_confirmed","execution_authorized")),"RECEIPT_CONTRACT")
    need((r["source_kind"]=="SYNTHETIC_TEST_ONLY" and r["research_eligible"] is False) if fixture else
         (r["source_kind"]=="ARCHIVE_REPLAY" and r["research_eligible"] is True),"SOURCE_KIND")
    need(set(r["tables"])==set(TABLES),"TABLE_INVENTORY")
    identities={}
    for table in TABLES:
        file=root/(table+".parquet");identities[table]=identity(file)
        entry=r["tables"][table]
        need(type(entry["rows"]) is int and entry["rows"]>=0 and hex64(entry["file_sha256"])
             and hex64(entry["canonical_rows_sha256"]) and digest(file)==entry["file_sha256"]
             and identity(file)==identities[table],"TABLE_PIN")
    db=duckdb.connect(":memory:",config={"memory_limit":"128MB","threads":"2"})
    try:
        for table in TABLES:
            n=db.execute("SELECT count(*) FROM read_parquet(?)",[str(root/(table+".parquet"))]).fetchone()[0]
            need(n==r["tables"][table]["rows"],"TABLE_COUNT")
        n,lo,hi,seq0,seq1,source_bad,epoch_bad,connection_bad=db.execute(
            """SELECT count(*),min(receiver_ms),max(receiver_ms),min(sequence),max(sequence),
              count(*) FILTER (WHERE source_manifest_sha256 IS DISTINCT FROM ?),
              count(*) FILTER (WHERE epoch IS DISTINCT FROM ?),
              count(*) FILTER (WHERE connection_id IS DISTINCT FROM 0)
              FROM read_parquet(?)""",[r["source_manifest_sha256"],r["epoch"],str(root/"packets.parquet")]).fetchone()
        need(n==r["source_messages"] and n>0 and 0<lo<=hi<2**53 and seq0==0 and seq1==n-1
             and source_bad==epoch_bad==connection_bad==0,"PACKET_INVENTORY")
    finally:db.close()
    for table in TABLES:
        need(identity(root/(table+".parquet"))==identities[table],"TABLE_CHANGED")
    need(identity(receipt)==before and receipt.read_bytes()==raw,"RECEIPT_CHANGED")
    return {"dataset_receipt_sha256":pin,"source_manifest_sha256":r["source_manifest_sha256"],
            "epoch":r["epoch"],"role":role,"source_kind":r["source_kind"],"first_receiver_ms":lo,
            "last_receiver_ms":hi,"source_messages":n,
            "table_sha256":{table:r["tables"][table]["file_sha256"] for table in TABLES}}

def catalog(entries,embargo_ms,*,fixture=False):
    need(type(entries) is list and 1<=len(entries)<=MAX_SESSIONS and type(embargo_ms) is int
         and 0<=embargo_ms<=86400000,"CATALOG_BOUNDS")
    rows=[];seen=set()
    for e in entries:
        need(type(e) is dict and set(e)=={"dataset_path","dataset_receipt_sha256","role"},"ENTRY")
        row=inspect(e["dataset_path"],e["dataset_receipt_sha256"],e["role"],fixture)
        need(row["source_manifest_sha256"] not in seen,"DUPLICATE_SOURCE")
        seen.add(row["source_manifest_sha256"]);rows.append(row)
    rows.sort(key=lambda r:r["first_receiver_ms"])
    for a,b in zip(rows,rows[1:]):
        need(a["last_receiver_ms"]<b["first_receiver_ms"],"OVERLAP")
        need(ROLES[a["role"]]<=ROLES[b["role"]],"ROLE_ORDER")
        if a["role"]!=b["role"]:
            need(b["first_receiver_ms"]-a["last_receiver_ms"]>embargo_ms,"EMBARGO")
    out={"schema":"h4_native_cross_session_catalog_v1","source_kind":"SYNTHETIC_TEST_ONLY" if fixture else "ARCHIVE_REPLAY",
         "session_count":len(rows),"source_messages":sum(r["source_messages"] for r in rows),
         "sessions":rows,"role_counts":{role:sum(r["role"]==role for r in rows) for role in ROLES},
         "embargo_ms":embargo_ms,"nonoverlapping_source_intervals":True,"cross_epoch_continuity":False,
         "holdout_access_history_verified":False,"statistical_independence_claim":False,
         "oos_approved":False,"alpha_confirmed":False,"execution_authorized":False}
    out["catalog_sha256"]=hashlib.sha256(canonical(out)).hexdigest()
    return out

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("plan",type=Path);p.add_argument("--output",type=Path,required=True)
    a=p.parse_args();need(a.plan.stat().st_size<=1000000,"PLAN_SIZE")
    plan=strict_json(a.plan.read_bytes());need(set(plan)=={"sessions","embargo_ms"},"PLAN")
    result=catalog(plan["sessions"],plan["embargo_ms"])
    with a.output.open("x") as f:json.dump(result,f,sort_keys=True,allow_nan=False);f.write("\n")
    print("H4_LOCAL_NATIVE_CATALOG_PASS")
if __name__=="__main__":main()
