"""Exact prior 180s gate and same-run redacted 30m artifact verification."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from q29_artifact_transport import extract, fetch_bytes, need
from q293_pilot_transport import validate as validate_180

PRIOR_ID = 14892285309
PRIOR_RUN = 45329896702
PRIOR_SHA = "3c835b92aede59494caaa3d15ab55836de96bb57"
PRIOR_ZIP = "0d14991439c3db0808f760f2a4808679089d6f0ae5207af3650e3b5358a479fb"
PRIOR_RECEIPT = "2d037794a6bc8daeadbf4db4f42f83aa9bf01ccce77c04f7cd1685083e2a69fb"
NAME = "q293-dual-1800s-baseline"
FILE = "dual_1800s.json"


def validate(root, digest, sha, run_id):
    raw = (root / FILE).read_bytes()
    need(hashlib.sha256(raw).hexdigest() == digest, "B4_RECEIPT_DIGEST")
    x = json.loads(raw)
    need(x["schema"] == "q293_dual_1800s_baseline_v1" and x["status"] == "PASS"
         and x["source_sha"] == sha and x["run_id"] == run_id
         and x["duration_seconds"] == 1800 and x["real_orders_enabled"] is False
         and x["five_hour_certified"] is False
         and x["resilience_5h_certified"] is False
         and x["exchange_attested"] is False
         and x["unavailable_middle_samples"] == 0
         and x["both_ready_samples"] >= 3000 and x["http_sse_checks"] >= 100
         and x["peak_process_count"] >= 5 and x["max_poll_gap_seconds"] <= 5
         and x["healthy_span_seconds"] >= 1770
         and 0 < x["peak_process_tree_rss_bytes"] <= 3 * 1024**3
         and x["min_free_disk_bytes"] >= 4 * 1024**3
         and x["ledger"]["torn_tail"] is False
         and x["source_ledger"]["torn_tail"] is False
         and set(x["archives"]) == {"primary", "secondary"}
         and set(x["epoch_audits"]) ==
         {"primary:attempt_01", "secondary:attempt_01"}, "B4_RECEIPT_GATE")
    for name, data in x["archives"].items():
        epoch = x["epoch_audits"][name + ":attempt_01"]
        need(data["independent_replay_agrees"] is True
             and data["close_trace_verified"] is True
             and data["source_span_seconds"] >= 1795
             and data["messages"] > 0 and data["chunks"] > 0
             and epoch["manifest_sha256"] == data["manifest_sha256"]
             and epoch["sealed_messages"] == data["messages"]
             and epoch["unsealed_entries_excluded"] == 0
             and epoch["zero_gap_certified"] is False
             and len(epoch["epochs"]) == 1
             and epoch["epochs"][0]["book_sha256"] ==
             data["independent_books_sha256"], "B4_ARCHIVE_RECEIPT")
    need(set(x["trade_id_crosscheck"]) == {"BTC-USD", "ETH-USD"},
         "B4_TRADE_PRODUCTS")
    for data in x["trade_id_crosscheck"].values():
        need(data["common_ids"] > 0 and data["divergent_ids"] == 0
             and data["matching_ids"] == data["common_ids"]
             and data["l2_equivalence_certified"] is False, "B4_TRADE_GATE")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--prior", action="store_true")
    p.add_argument("--id", type=int)
    p.add_argument("--zip-sha")
    p.add_argument("--receipt-sha")
    p.add_argument("--directory", required=True, type=Path)
    args = p.parse_args()
    need(os.environ["GITHUB_RUN_ATTEMPT"] == "1", "B4_RERUN_BLOCKED")
    repo, token = os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"]
    if args.prior:
        raw = fetch_bytes(repo, token, artifact_id=PRIOR_ID,
                          name="q293-dual-180s-baseline", run_id=PRIOR_RUN,
                          sha=PRIOR_SHA, zip_sha=PRIOR_ZIP)
        extract(raw, args.directory, {"dual_180s.json"},
                lambda root: validate_180(root, PRIOR_RECEIPT, PRIOR_SHA, PRIOR_RUN))
        print("PASS_B4_PRIOR_180S_EXACT_ARTIFACT")
    else:
        sha, run = os.environ["GITHUB_SHA"], int(os.environ["GITHUB_RUN_ID"])
        raw = fetch_bytes(repo, token, artifact_id=args.id, name=NAME,
                          run_id=run, sha=sha, zip_sha=args.zip_sha)
        extract(raw, args.directory, {FILE},
                lambda root: validate(root, args.receipt_sha, sha, run))
        print("PASS_B4_1800S_ARTIFACT_ROUNDTRIP")


if __name__ == "__main__":
    main()
