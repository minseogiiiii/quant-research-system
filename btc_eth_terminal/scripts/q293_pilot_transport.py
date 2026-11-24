"""Verify the exact redacted dual pilot artifact by ID and SHA-256."""
import argparse
import hashlib
import json
import os
from pathlib import Path

from q29_artifact_transport import extract, fetch_bytes, need

NAME = "q293-dual-180s-baseline"
FILE = "dual_180s.json"


def validate(path, digest, sha, run_id):
    raw = (path / FILE).read_bytes()
    need(hashlib.sha256(raw).hexdigest() == digest, "PILOT_RECEIPT_DIGEST")
    x = json.loads(raw)
    need(x["schema"] == "q293_dual_180s_baseline_v1"
         and x["source_sha"] == sha and x["run_id"] == run_id
         and x["duration_seconds"] == 180 and x["status"] == "PASS"
         and x["real_orders_enabled"] is False
         and x["five_hour_certified"] is False
         and x["resilience_5h_certified"] is False
         and x["unavailable_middle_samples"] == 0
         and x["healthy_samples"] >= 300
         and set(x["archives"]) == {"primary", "secondary"}
         and x["ledger"]["torn_tail"] is False, "PILOT_RECEIPT_GATE")
    for data in x["archives"].values():
        need(data["independent_replay_agrees"] is True
             and data["close_trace_verified"] is True
             and data["source_span_seconds"] >= 175
             and data["messages"] > 0 and data["chunks"] > 0,
             "PILOT_ARCHIVE_RECEIPT")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--id", required=True, type=int)
    p.add_argument("--zip-sha", required=True)
    p.add_argument("--receipt-sha", required=True)
    p.add_argument("--directory", required=True, type=Path)
    args = p.parse_args()
    sha = os.environ["GITHUB_SHA"]
    run_id = int(os.environ["GITHUB_RUN_ID"])
    need(os.environ["GITHUB_RUN_ATTEMPT"] == "1", "PILOT_RERUN_BLOCKED")
    raw = fetch_bytes(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"],
                      artifact_id=args.id, name=NAME, run_id=run_id, sha=sha,
                      zip_sha=args.zip_sha)
    extract(raw, args.directory, {FILE},
            lambda root: validate(root, args.receipt_sha, sha, run_id))
    print("PASS_Q293_DUAL_180S_ARTIFACT_ROUNDTRIP")


if __name__ == "__main__":
    main()
