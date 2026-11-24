"""Historical frozen diagnostic and separate new public forward engineering pilot."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from q13_private_fixture_gate import verify_zip,PREFIX
from independent_soak_replay import strict_json
from f6_liquidity_recovery import build,POLICY,digest
PIN="8f2e0ec88a601a92ab4c7bcd0e04b20536fbe46bbd8b3c7f67ae3c1d075c28e5"
SOURCE=PREFIX+"backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy/"
PRODUCTS=("BTC-USD","ETH-USD")
def require(ok,reason):
    if not ok:raise ValueError(reason)
def summarize(root,pin,kind):
    reports={p:build(root,pin,p) for p in PRODUCTS}
    require(all(r["policy_sha256"]==digest(POLICY) and r["independent_input_parser_parity"] for r in reports.values()),"F6_POLICY_PARITY")
    require(all(r["source_last_ms"]-r["source_first_ms"]>=110000 and r["valid_decisions"]>=50 for r in reports.values()),"F6_SOURCE_COVERAGE")
    return {"schema":"f6_recovery_real_pilot_v1","source_kind":kind,"status":"ENGINEERING_DIAGNOSTIC_PASS",
        "source_manifest_sha256":pin,"source_sha":os.environ.get("GITHUB_SHA"),
        "policy_sha256":digest(POLICY),"policy":POLICY,
        "products":{p:{k:r[k] for k in ("source_first_ms","source_last_ms","source_messages","valid_decisions","missing_decisions","signals","entries","closed_roundtrips","unresolved_position","realized_net_quote","final_cash_if_flat","independent_input_parser_parity")} for p,r in reports.items()},
        "raw_data_uploaded":False,"actual_fills_verified":False,"oos_approved":False,"alpha_confirmed":False,"execution_authorized":False}
def historical(archive):
    with tempfile.TemporaryDirectory(prefix="f6-historical-") as d:
        root=Path(d)
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
        return summarize(root,PIN,"PREVIOUSLY_INSPECTED_HISTORICAL")
def forward(prior):
    require(prior["source_kind"]=="PREVIOUSLY_INSPECTED_HISTORICAL" and prior["status"]=="ENGINEERING_DIAGNOSTIC_PASS"
            and prior["source_manifest_sha256"]==PIN and prior["policy_sha256"]==digest(POLICY)
            and prior["source_sha"]==os.environ.get("GITHUB_SHA"),"F6_PRIOR_POLICY_SOURCE")
    require(os.environ.get("GITHUB_RUN_ATTEMPT")=="1","F6_NO_RETRY")
    with tempfile.TemporaryDirectory(prefix="f6-forward-") as d:
        work=Path(d)
        collector=Path(__file__).resolve().with_name("btc_eth_multiday_v20.mjs")
        env={"PATH":os.environ["PATH"],"SOAK_DURATION_SECONDS":"120","SOAK_RAW_BUDGET_BYTES":"1000000000",
             "SOAK_ARCHIVE_BUDGET_BYTES":"1000000000","SOAK_DISK_RESERVE_BYTES":"1073741824","SOAK_CHUNK_LIMIT_BYTES":"8000000"}
        proc=subprocess.run(["node",str(collector)],cwd=work,env=env,capture_output=True,timeout=210)
        require(proc.returncode==0,"F6_FORWARD_CAPTURE_NOT_CLEAN")
        roots=list((work/"soak_evidence").glob("coinbase_btc_eth_soak_*"));require(len(roots)==1,"F6_FORWARD_INVENTORY")
        root=roots[0];raw=(root/"manifest.json").read_bytes();m=strict_json(raw)
        require(m.get("synthetic_fixture") is False and m.get("controlled_fault_injection") is None
                and m.get("finish_reason")=="DURATION_REACHED" and m.get("total_reconnects")==0,"F6_FORWARD_CLEAN")
        pin=hashlib.sha256(raw).hexdigest();require(pin!=PIN,"F6_FORWARD_DISTINCT")
        result=summarize(root,pin,"NEW_PUBLIC_FORWARD_CAPTURE")
        require(all(result["products"][p]["source_first_ms"]>prior["products"][p]["source_last_ms"] for p in PRODUCTS),"F6_FORWARD_TIME")
        result["historical_manifest_sha256"]=PIN
        result["frozen_policy_matches_historical"]=True
        result["post_freeze_distinct_source"]=True
        return result
def main():
    p=argparse.ArgumentParser();p.add_argument("--release-zip",type=Path);p.add_argument("--historical-receipt",type=Path);p.add_argument("--output",type=Path,required=True)
    a=p.parse_args();require(bool(a.release_zip)!=bool(a.historical_receipt),"F6_MODE")
    result=historical(a.release_zip) if a.release_zip else forward(strict_json(a.historical_receipt.read_bytes()))
    with a.output.open("x") as f:json.dump(result,f,sort_keys=True);f.write("\n")
    print("F6_"+result["source_kind"]+"_DIAGNOSTIC_PASS")
if __name__=="__main__":main()
