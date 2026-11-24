"""Immutable B4.1 prerequisite and B5 redacted receipt roundtrip."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from q29_artifact_transport import extract, fetch_bytes, need
from q293_b4_transport import validate as validate_b4

PRIOR_ID = 14908322306
PRIOR_RUN = 45364185521
PRIOR_SHA = "1eae31cc3a7b44cd0fd4a40983e98708fd0937a3"
PRIOR_ZIP = "69db44fe1a707c434fc39090ff7918b340bab7fe875de0fd4c0ad1b0b168d92b"
PRIOR_RECEIPT = "31683d14852910cf102aeae5174d412e9133d0b98f1923d709b390335c298e82"
NAME = "q293-b5-live-resilience"
FILE = "b5.json"


def validate(root, digest, sha, run):
    raw = (root / FILE).read_bytes()
    need(hashlib.sha256(raw).hexdigest() == digest, "B5_RECEIPT_DIGEST")
    x = json.loads(raw)
    need(x["schema"] == "q293_b5_resilience_v1" and x["status"] == "PASS"
         and x["failure"] is None and x["synthetic"] is False
         and x["duration_seconds"] == 1800 and x["observed_seconds"] >= 1800
         and x["source_sha"] == sha and x["run_id"] == run
         and all(x[k] is False for k in ("real_orders_enabled", "zero_gap_certified",
                 "historical_gap_recovered", "resilience_5h_certified",
                 "exchange_attested"))
         and len(x["faults"]) == 2
         and {v["source"] for v in x["faults"]} == {"primary", "secondary"}
         and len(x["client_frames"]) == 8 and min(x["client_frames"]) >= 500
         and x["both_ready_samples"] >= 5000 and x["max_poll_gap_seconds"] <= 5
         and 0 < x["peak_rss_bytes"] <= 3 * 1024**3
         and x["min_free_disk_bytes"] >= 4 * 1024**3
         and x["route_ledger"]["torn_tail"] is False
         and x["source_ledger"]["torn_tail"] is False
         and x["source_ledger"]["attempts"] == {"primary": 2, "secondary": 2},
         "B5_RECEIPT_GATE")
    for fault in x["faults"]:
        need(0 <= fault["promotion_seconds"] <= 5
             and fault["promotion_seconds"] <= fault["recovery_seconds"] <= 20
             and fault["resync_clients"] == 8 and fault["stale_cursor_rejected"] is True
             and fault["fresh_standby_observed"] is True
             and fault["new_generation"] > fault["old_generation"], "B5_FAULT_GATE")
    need(set(x["archives"]) == {f"{s}:attempt_0{i}" for s in ("primary", "secondary")
                               for i in (1, 2)}, "B5_ARCHIVE_SET")
    for item in x["archives"].values():
        a = item["audit"]
        need(a["sealed_messages"] > 0 and len(a["epochs"]) == 1
             and a["epochs"][0]["both_books_initialized_and_updated"] is True
             and a["zero_gap_certified"] is False, "B5_ARCHIVE_GATE")
    need(set(x["trade_id_crosscheck"]) == {"BTC-USD", "ETH-USD"}, "B5_TRADE_SET")
    for item in x["trade_id_crosscheck"].values():
        need(item["common_ids"] > 0 and item["divergent_ids"] == 0
             and item["matching_ids"] == item["common_ids"]
             and item["l2_equivalence_certified"] is False, "B5_TRADE_GATE")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--prior", action="store_true")
    p.add_argument("--id", type=int)
    p.add_argument("--zip-sha")
    p.add_argument("--receipt-sha")
    p.add_argument("--directory", type=Path, required=True)
    a = p.parse_args()
    need(os.environ["GITHUB_RUN_ATTEMPT"] == "1", "B5_RERUN_BLOCKED")
    repo, token = os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"]
    if a.prior:
        raw = fetch_bytes(repo, token, artifact_id=PRIOR_ID,
                          name="q293-dual-1800s-baseline", run_id=PRIOR_RUN,
                          sha=PRIOR_SHA, zip_sha=PRIOR_ZIP)
        extract(raw, a.directory, {"dual_1800s.json"},
                lambda root: validate_b4(root, PRIOR_RECEIPT, PRIOR_SHA, PRIOR_RUN))
        print("PASS_B5_PRIOR_B4_EXACT_ARTIFACT")
    else:
        sha, run = os.environ["GITHUB_SHA"], int(os.environ["GITHUB_RUN_ID"])
        raw = fetch_bytes(repo, token, artifact_id=a.id, name=NAME, run_id=run,
                          sha=sha, zip_sha=a.zip_sha)
        extract(raw, a.directory, {FILE},
                lambda root: validate(root, a.receipt_sha, sha, run))
        print("PASS_B5_ARTIFACT_ROUNDTRIP")


if __name__ == "__main__":
    main()
