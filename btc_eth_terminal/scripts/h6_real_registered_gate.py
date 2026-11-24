"""Previously inspected source: verify real registered backtest execution."""
import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from q13_private_fixture_gate import verify_zip,PREFIX
from independent_soak_replay import strict_json
from h1_streaming_parquet import convert
from h6_registered_strategy_runner import run
from h5_experiment_registry import snapshot,sha
from h8_qualification import assess
PIN="8f2e0ec88a601a92ab4c7bcd0e04b20536fbe46bbd8b3c7f67ae3c1d075c28e5"
SOURCE=PREFIX+"backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy/"
def require(ok,reason):
    if not ok:raise ValueError(reason)
def gate(archive,output):
    with tempfile.TemporaryDirectory(prefix="h6-real-") as d:
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
        entry={"dataset_path":str(dest),"dataset_receipt_sha256":hashlib.sha256((dest/"dataset.json").read_bytes()).hexdigest(),"role":"DEVELOPMENT"}
        journal=Path(d)/"journal.db"
        r=run([entry],0,[{"product":"BTC-USD","hold_ms":5000},{"product":"BTC-USD","hold_ms":10000}],42,journal)
        s=snapshot(journal,r["plan_sha256"],r["registry_head_sha256"])
        require(s["all_terminal"] and len(r["results"])==2,"H6_TERMINAL")
        require(r["results"][0]["status"]=="COMPLETE","H6_BASELINE_INCOMPLETE")
        require(r["results"][0]["sessions"][0]["closed_roundtrips"]==5,"H6_BASELINE_TRADE_COUNT")
        require(abs(r["results"][0]["sessions"][0]["net_return"]-(-7.651274/10000))<1e-8,"H6_BASELINE_CASH")
        r.update(status="ENGINEERING_DIAGNOSTIC_PASS",raw_data_uploaded=False,source_kind="PREVIOUSLY_INSPECTED_HISTORICAL")
        qualification=assess(r,journal,r["plan_sha256"],r["registry_head_sha256"],sha(r))
        require(all(t["verdict"]=="REJECTED_DEVELOPMENT" for t in qualification["trials"]),"H8_NEGATIVE_DEVELOPMENT")
        r["qualification"]=qualification
        with output.open("x") as f:json.dump(r,f,sort_keys=True,allow_nan=False);f.write("\n")
        print("H6_REAL_REGISTERED_RUNNER_PASS")
if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--release-zip",type=Path,required=True);p.add_argument("--output",type=Path,required=True)
    a=p.parse_args();gate(a.release_zip,a.output)
