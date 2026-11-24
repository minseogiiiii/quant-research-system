"""Use the existing frozen Q7 release and exact 120s source manifest pin.

Never upload or print raw source or derived prices. No skip on missing source.
"""
import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from q13_private_fixture_gate import verify_zip, PREFIX
from f4_ofi_markout import build
from f4_ofi_independent import audit
from f4_order_flow import read_packets
from independent_soak_replay import strict_json

PIN="8f2e0ec88a601a92ab4c7bcd0e04b20536fbe46bbd8b3c7f67ae3c1d075c28e5"
SOURCE=PREFIX+"backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy/"

def require(ok,code):
    if not ok:
        raise ValueError(code)

def run(archive,output):
    with tempfile.TemporaryDirectory(prefix="f4_private_real_") as temp:
        root=Path(temp)
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
        first=last=None
        for _,t,_ in read_packets(root,m):
            first=t if first is None else first
            last=t
        require(first is not None and last-first>=110000,"F4_REAL_DURATION")
        start=((first+10000+999)//1000)*1000
        end=(last-10000)//1000*1000
        receipts={}
        for p in ("BTC-USD","ETH-USD"):
            report=build(root,PIN,product=p,start_ms=start,end_ms=end,step_ms=1000,
                         window_ms=5000,max_age_ms=3000,horizons_ms=[100,1000,5000],tolerance_ms=1500)
            receipt=audit(read_packets(root,m),report)
            require(receipt["valid_features"]>=50 and receipt["observed_labels"]>=100,"F4_REAL_SAMPLE_COVERAGE")
            receipts[p]=receipt
        result={"schema":"f4_real_archive_ofi_witness_v1","status":"PASS","source_manifest_sha256":PIN,
                "products":receipts,"synthetic":False,"raw_data_uploaded":False,
                "oos_approved":False,"alpha_confirmed":False,"execution_authorized":False}
        with output.open("x") as f:
            json.dump(result,f,sort_keys=True)
            f.write("\n")
        print("F4_REAL_ARCHIVE_OFI_WITNESS_PASS")

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--release-zip",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    args=p.parse_args()
    run(args.release_zip,args.output)
