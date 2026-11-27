"""Pinned B5 prerequisite and strict C1 receipt validation."""
import argparse
import hashlib
import json
import os
from pathlib import Path

from q29_artifact_transport import extract, fetch_bytes, need
from q293_b5_transport import validate as validate_b5

B5_ID=14914680461
B5_RUN=45376286921
B5_SHA="ee461b924ef37a1128d94f3a9078cb41d4ea204f"
B5_ZIP="2a1d475d371db4e9cfabf2155f35cc7bf348822bdf922f406738cbda0f5144ac"
B5_RECEIPT="dff41f3c171450dd398121451e371c9762804369391c297213ef92c477aa22c7"

def validate(root, digest, sha, run):
    raw=(root/"c1.json").read_bytes()
    need(hashlib.sha256(raw).hexdigest()==digest,"C1_DIGEST")
    x=json.loads(raw)
    need(x["schema"]=="q293_c1_dual_18000s_baseline_v1" and x["status"]=="PASS"
         and x["source_sha"]==sha and x["run_id"]==run and x["duration_seconds"]==18000
         and x["five_hour_certified"] is True and all(x[k] is False for k in (
             "zero_gap_certified","resilience_5h_certified","real_orders_enabled","exchange_attested"))
         and x["healthy_span_seconds"]>=17970 and x["unavailable_middle_samples"]==0
         and x["healthy_samples"]>=30000 and x["both_ready_samples"]>=30000
         and x["http_sse_checks"]>=1000 and x["max_poll_gap_seconds"]<=5
         and x["peak_process_tree_rss_bytes"]<=3*1024**3
         and x["min_free_disk_bytes"]>=4*1024**3
         and x["checkpoint_comparison_counts"]["DIVERGENCE"]==0
         and x["ledger"]["torn_tail"] is False and x["source_ledger"]["torn_tail"] is False
         and x["source_ledger"]["attempts"]=={"primary":1,"secondary":1},"C1_GATE")
    need(set(x["archives"])=={"primary","secondary"},"C1_ARCHIVES")
    for a in x["archives"].values():
        need(a["source_span_seconds"]>=17995 and a["messages"]>0 and a["chunks"]>0
             and a["independent_replay_agrees"] is True and a["close_trace_verified"] is True,
             "C1_ARCHIVE_GATE")
    need(set(x["epoch_audits"])=={"primary:attempt_01","secondary:attempt_01"},"C1_EPOCH_SET")
    for key,a in x["epoch_audits"].items():
        ref=x["archives"][key.split(":")[0]]
        need(a["unsealed_entries_excluded"]==0 and a["manifest_tmp_excluded"] is False
             and len(a["epochs"])==1 and a["epochs"][0]["both_books_initialized_and_updated"] is True
             and a["manifest_sha256"]==ref["manifest_sha256"] and a["sealed_messages"]==ref["messages"]
             and a["epochs"][0]["book_sha256"]==ref["independent_books_sha256"],"C1_EPOCH_GATE")
    need(set(x["trade_id_crosscheck"])=={"BTC-USD","ETH-USD"},"C1_TRADES")
    for a in x["trade_id_crosscheck"].values():
        need(a["common_ids"]>0 and a["matching_ids"]==a["common_ids"] and a["divergent_ids"]==0
             and a["l2_equivalence_certified"] is False,"C1_TRADE_GATE")

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--prior",action="store_true")
    p.add_argument("--id",type=int)
    p.add_argument("--zip-sha")
    p.add_argument("--receipt-sha")
    p.add_argument("--directory",type=Path,required=True)
    a=p.parse_args()
    need(os.environ["GITHUB_RUN_ATTEMPT"]=="1","C1_RERUN_BLOCKED")
    repo,token=os.environ["GITHUB_REPOSITORY"],os.environ["GH_TOKEN"]
    if a.prior:
        raw=fetch_bytes(repo,token,artifact_id=B5_ID,name="q293-b5-live-resilience",
                        run_id=B5_RUN,sha=B5_SHA,zip_sha=B5_ZIP)
        extract(raw,a.directory,{"b5.json"},lambda root:validate_b5(root,B5_RECEIPT,B5_SHA,B5_RUN))
        print("PASS_C1_PRIOR_B5_EXACT_ARTIFACT")
    else:
        sha,run=os.environ["GITHUB_SHA"],int(os.environ["GITHUB_RUN_ID"])
        raw=fetch_bytes(repo,token,artifact_id=a.id,name="q293-c1-dual-18000s-baseline",
                        run_id=run,sha=sha,zip_sha=a.zip_sha)
        extract(raw,a.directory,{"c1.json"},lambda root:validate(root,a.receipt_sha,sha,run))
        print("PASS_C1_ARTIFACT_ROUNDTRIP")

if __name__=="__main__":
    main()
