"""Offline admission audit of immutable P0 receipts. No market I/O."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
from pathlib import Path

from q29_artifact_transport import extract, fetch_bytes
from q293_b5_transport import validate as validate_b5
from q293_epoch_audit import MAX_CHUNKS, MAX_MESSAGES, MAX_TRADES
from q293_long_policy import DISK_RESERVE, GIB, admission, budgets

SOURCE = "1ce7d18f914973c04d73d6fe16afc1aea507f90a"
RUN = 45980525209
B5_ID = 15192056636
B5_ZIP = "2430dde2c73ab76f54f08202ab73564753b5c10d81a14521718e103aa3a81a96"
B5_JSON = "e8cf00490ec42ec3b5d3fc05cca80f5504aa167b8693bb0d13cce0763bbc94ca"
C2_ID = 15193129601
C2_ZIP = "aef59cc961b89e1c3ae04887317b610f0cb8e2a2953a8a7841db4da77916c87e"
C2_JSON = "076f6c00b5f79f7c484a4ab59d0375f79f7cd65a5d7c8860ab129d63e9e8d73f"
PRODUCTS = {"BTC-USD", "ETH-USD"}
ATTEMPTS = {"primary": 2, "secondary": 2}
ARCHIVES = {f"{s}:attempt_0{i}" for s in ATTEMPTS for i in (1, 2)}
HEX = re.compile(r"[0-9a-f]{64}")
HORIZON = 18000
MARGIN = 2
ALLOWED_DIFF = {
    "btc_eth_terminal/scripts/q297_p0_preflight.py",
    ".github/scripts/test_q297_p0_preflight.py",
    ".github/workflows/q297_p0_preflight.yml",
    ".github/workflows/q298_capacity_stress.yml",
    "btc_eth_terminal/scripts/q298_capacity_stress.py",
    "btc_eth_terminal/tests/test_q298_capacity_stress.py",
}


def need(ok, code):
    if not ok:
        raise ValueError(code)


def number(value, minimum, maximum):
    return (type(value) in (int, float) and math.isfinite(value)
            and minimum <= value <= maximum)


def count(value, minimum=0, maximum=10**12):
    return type(value) is int and minimum <= value <= maximum


def hex64(value):
    return isinstance(value, str) and HEX.fullmatch(value) is not None


def pin_file(path, expected):
    raw = path.read_bytes()
    need(hashlib.sha256(raw).hexdigest() == expected, "PREFLIGHT_RECEIPT_PIN")
    return json.loads(raw)


def unchanged_runtime():
    changed = set(subprocess.check_output(
        ["git", "diff", "--name-only", SOURCE, "HEAD"], text=True
    ).splitlines())
    need(changed <= ALLOWED_DIFF, "PREFLIGHT_RUNTIME_DIFF")


def check_archives(receipt):
    need(set(receipt["archives"]) == ARCHIVES, "PREFLIGHT_ARCHIVE_SET")
    total_messages = total_bytes = 0
    rates = []
    for item in receipt["archives"].values():
        a = item["audit"]
        manifest = item.get("manifest", item.get("faults"))
        need(a["schema"] == "q293_sealed_epoch_forensics_v1"
             and hex64(a["manifest_sha256"])
             and manifest["status"] == "MANIFEST_READ"
             and a["manifest_sha256"] == manifest["manifest_sha256"]
             and manifest["connections"] == 1
             and manifest["faults"] == []
             and a["manifest_tmp_excluded"] is False
             and a["unsealed_entries_excluded"] == 0
             and item["observer"]["status"] == "VERIFIED"
             and hex64(item["observer"]["terminal_sha256"])
             and all(a[k] is False for k in (
                 "zero_gap_certified", "historical_gap_recovered",
                 "real_orders_enabled"))
             and len(a["epochs"]) == 1, "PREFLIGHT_ARCHIVE_INTEGRITY")
        e = a["epochs"][0]
        need(count(a["sealed_messages"], 1, MAX_MESSAGES)
             and count(a["sealed_bytes"], 1)
             and count(a["sealed_chunks"], 1, MAX_CHUNKS)
             and e["messages"] == a["sealed_messages"]
             and e["sealed_prefix_replayed"] is True
             and e["complete_epoch_certified"] is False
             and e["both_books_initialized_and_updated"] is True
             and set(e["book_sha256"]) == PRODUCTS
             and all(hex64(h) for h in e["book_sha256"].values())
             and count(e["heartbeat_messages"], 1)
             and count(e["first_monotonic_ns"], 1, 10**20)
             and count(e["last_monotonic_ns"], 1, 10**20),
             "PREFLIGHT_EPOCH_INTEGRITY")
        span = (e["last_monotonic_ns"] - e["first_monotonic_ns"]) / 1e9
        need(250 <= span <= 1820, "PREFLIGHT_PILOT_ARCHIVE_SPAN")
        rates.append((a["sealed_messages"] / span, a["sealed_bytes"] / span,
                      a["sealed_chunks"] / span))
        total_messages += a["sealed_messages"]
        total_bytes += a["sealed_bytes"]
    need(receipt["source_ledger"]["attempts"] == ATTEMPTS
         and receipt["route_ledger"]["torn_tail"] is False
         and receipt["source_ledger"]["torn_tail"] is False
         and all(hex64(receipt[k]["terminal_sha256"])
                 for k in ("route_ledger", "source_ledger")),
         "PREFLIGHT_LEDGER")
    return total_messages, total_bytes, rates


def check_trades(receipt):
    cross = receipt["trade_id_crosscheck"]
    need(set(cross) == PRODUCTS, "PREFLIGHT_TRADE_PRODUCTS")
    total = overlap = 0
    for value in cross.values():
        p, s, n = (value[k] for k in ("primary_ids", "secondary_ids", "common_ids"))
        need(all(count(v, 1) for v in (p, s, n))
             and n <= min(p, s)
             and value["divergent_ids"] == 0
             and value["matching_ids"] == n
             and value["l2_equivalence_certified"] is False, "PREFLIGHT_TRADE_JOIN")
        total += p + s
        overlap += n
    return total, overlap


def validate_c2(c2):
    need(c2["schema"] == "q296_c2_resilience_v1"
         and c2["status"] == "PASS" and c2["failure"] is None
         and c2["source_sha"] == SOURCE and c2["run_id"] == RUN
         and c2["synthetic"] is False and c2["duration_seconds"] == 1800
         and number(c2["observed_seconds"], 1800, 1805)
         and all(c2[k] is False for k in (
             "real_orders_enabled", "zero_gap_certified", "historical_gap_recovered",
             "exchange_attested", "execution_authorized",
             "c1_clean_baseline_certified", "five_hour_resilience_certified")),
         "PREFLIGHT_C2_IDENTITY")
    a = c2["availability"]
    need(a["duration_seconds"] == 1800
         and a["observed_seconds"] == c2["observed_seconds"]
         and number(a["startup_seconds"], 0, 20)
         and count(a["samples"], 3600) and count(a["both_ready_samples"], 3600)
         and a["both_ready_samples"] <= a["samples"]
         and number(a["max_poll_gap_seconds"], 0, 5)
         and number(a["outage_upper_bound_seconds"], 0, 20)
         and number(a["degraded_upper_bound_seconds"], 0, 120)
         and a["outage_upper_bound_seconds"] <= (1800 - a["startup_seconds"]) * .005
         and a["degraded_upper_bound_seconds"] <= (1800 - a["startup_seconds"]) * .01
         and a["source_attempts"] == ATTEMPTS
         and a["cursor_transitions"] == 1
         and len(a["recovered_incidents"]) == 2
         and len(c2["injections"]) == 2, "PREFLIGHT_C2_AVAILABILITY")
    for role, t, injection, recovery in zip(
        ("standby", "active"), (300, 900), c2["injections"],
        a["recovered_incidents"], strict=True
    ):
        need(injection["role"] == role
             and injection["source"] in ATTEMPTS
             and number(injection["at_seconds"], t, t + 20)
             and number(recovery["start_seconds"], t - 5, t + 25)
             and number(recovery["recovery_seconds"], 0, 20),
             "PREFLIGHT_C2_RECOVERY")
    need(len({v["source"] for v in c2["injections"]}) == 2
         and len(c2["client_frames"]) == 8
         and all(count(n, 500) for n in c2["client_frames"])
         and len(c2["client_connections"]) == 8
         and all(count(n, 1) for n in c2["client_connections"])
         and count(c2["http_checks"], 180)
         and number(c2["audit_seconds"], 0.001, 2400),
         "PREFLIGHT_C2_CLIENTS")


def audit(b5, c2):
    validate_c2(c2)
    totals = [check_archives(r) for r in (b5, c2)]
    trades = [check_trades(r) for r in (b5, c2)]
    need(c2["trade_index_rows"] == trades[1][0], "PREFLIGHT_INDEX_COUNT")
    need(all(number(r["peak_rss_bytes"], 1, 3 * GIB)
             and number(r["min_free_disk_bytes"], DISK_RESERVE, 10**14)
             for r in (b5, c2)), "PREFLIGHT_RESOURCES")
    rates = totals[0][2] + totals[1][2]
    # Maximum whole-attempt average rate, not a measured instantaneous peak.
    # Extrapolate to a full 5h per attempt, rather than treating a shorter
    # recovered attempt as a complete 30-minute measurement.
    messages = math.ceil(max(v[0] for v in rates) * HORIZON * MARGIN)
    archive_bytes = math.ceil(max(v[1] for v in rates) * HORIZON * MARGIN)
    chunks = math.ceil(max(v[2] for v in rates) * HORIZON * MARGIN)
    index = math.ceil(max(n for n, _ in trades) * HORIZON * MARGIN / 1800)
    seconds = math.ceil(c2["audit_seconds"] * HORIZON * MARGIN / 1800)
    rss = math.ceil(max(r["peak_rss_bytes"] for r in (b5, c2)) * MARGIN)
    _, archive_cap = budgets(HORIZON)
    disk = admission(HORIZON, 1)
    need(messages <= MAX_MESSAGES and archive_bytes <= archive_cap
         and chunks <= MAX_CHUNKS and index <= MAX_TRADES
         and seconds <= 2400 and rss <= 3 * GIB
         and min(r["min_free_disk_bytes"] for r in (b5, c2)) >= disk,
         "PREFLIGHT_CAPACITY")
    return {
        "schema": "q297_preflight_v1",
        "source_sha": SOURCE, "run_id": RUN,
        "b5_receipt_pin": B5_JSON, "c2_receipt_pin": C2_JSON,
        "observed_market_messages": sum(v[0] for v in totals),
        "observed_sealed_bytes": sum(v[1] for v in totals),
        "common_trade_ids_across_pilots": sum(n for _, n in trades),
        "projected_per_attempt_messages": messages,
        "projected_per_attempt_archive_bytes": archive_bytes,
        "projected_per_attempt_chunks": chunks,
        "projected_trade_index_rows": index,
        "projected_audit_seconds": seconds,
        "projected_runtime_rss_bytes": rss,
        "required_fresh_host_disk_bytes": disk,
        "prior_pilot_min_free_disk_bytes": min(
            r["min_free_disk_bytes"] for r in (b5, c2)),
        "empirical_capacity_estimate_pass": True,
        "resource_model": "2x maximum observed attempt-average rate",
        "instantaneous_peak_and_audit_rss_measured": False,
        "raw_archives_reaudited_here": False,
        "five_hour_approved": False,
        "real_orders_enabled": False, "zero_gap_certified": False,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--directory", type=Path, required=True)
    args = p.parse_args()
    unchanged_runtime()
    root = args.directory
    root.mkdir(mode=0o700)
    repo, token = os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"]
    for aid, name, zip_sha, filename, digest, dest in (
        (B5_ID, "q293-b5-live-resilience", B5_ZIP, "b5.json", B5_JSON, "b5"),
        (C2_ID, "p0-c2-pilot-receipt", C2_ZIP, "c2.json", C2_JSON, "c2"),
    ):
        raw = fetch_bytes(repo, token, artifact_id=aid, name=name, run_id=RUN,
                          sha=SOURCE, zip_sha=zip_sha)
        extract(raw, root / dest, {filename},
                lambda directory, f=filename, d=digest: pin_file(directory / f, d))
    validate_b5(root / "b5", B5_JSON, SOURCE, RUN)
    result = audit(pin_file(root / "b5/b5.json", B5_JSON),
                   pin_file(root / "c2/c2.json", C2_JSON))
    print("PREFLIGHT_EVIDENCE", json.dumps(result, sort_keys=True))
    (root / "preflight.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
