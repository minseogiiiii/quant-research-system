"""Q298 bounded offline capacity qualification; never opens a market socket."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import shutil
import time
from collections import deque
from pathlib import Path

from q13_private_fixture_gate import PREFIX, verify_zip
from q293_epoch_audit import TradeIndex, scan
from q293_long_policy import DISK_RESERVE, GIB, admission
from q297_p0_preflight import B5_JSON, C2_JSON, audit, pin_file

MANIFEST = "08255d79f0a7f081921f370feca2e92686cde3dffbb406eadc23e918d43e73d7"
REL = "backend/data/live_v16_one_hour/coinbase_btc_eth_soak_eE9SNH/"
MESSAGES = 152069
BYTES = 325724466
CHUNKS = 41
RSS_LIMIT = 3 * GIB
AUDIT_LIMIT = 2400
PRODUCTS = ("BTC-USD", "ETH-USD")


def need(ok, code):
    if not ok:
        raise ValueError(code)


def peak_rss():
    # Linux ru_maxrss is KiB. This is a process high-water mark, not sampling.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def check_limits(root, start):
    need(time.monotonic() - start <= AUDIT_LIMIT, "Q298_TIME_LIMIT")
    need(peak_rss() <= RSS_LIMIT, "Q298_MEMORY_LIMIT")
    need(shutil.disk_usage(root).free >= DISK_RESERVE, "Q298_DISK_RESERVE")


def extract_hour(zip_path, root):
    root.mkdir(mode=0o700)
    (root / "chunks").mkdir(mode=0o700)
    prefix = PREFIX + REL
    with verify_zip(zip_path) as archive:
        raw = archive.read(prefix + "manifest.json")
        need(hashlib.sha256(raw).hexdigest() == MANIFEST, "Q298_MANIFEST_PIN")
        m = json.loads(raw)
        need(m["messages"] == MESSAGES and len(m["chunks"]) == CHUNKS
             and m["resource_telemetry"]["archived_jsonl_bytes"] == BYTES
             and m["faults"] == [] and m["total_reconnects"] == 0
             and m["orders_enabled"] is False
             and m["finish_reason"] == "DURATION_REACHED",
             "Q298_HOUR_IDENTITY")
        (root / "manifest.json").write_bytes(raw)
        for i, entry in enumerate(m["chunks"]):
            name = f"chunks/chunk_{i:05d}_conn_000.jsonl"
            need(entry["file"] == name, "Q298_CHUNK_NAME")
            with archive.open(prefix + name) as src, (root / name).open("xb") as dst:
                shutil.copyfileobj(src, dst)
    return m


class Window:
    """Exact sliding trailing time window on nondecreasing receive timestamps."""

    def __init__(self, width_ns):
        self.width_ns = width_ns
        self.rows = deque()
        self.total = self.max_bytes = self.max_messages = 0
        self.last = -1

    def add(self, now, size):
        need(type(now) is int and now >= self.last and size > 0,
             "Q298_WINDOW_ORDER")
        self.last = now
        while self.rows and self.rows[0][0] <= now - self.width_ns:
            self.total -= self.rows.popleft()[1]
        self.rows.append((now, size))
        self.total += size
        self.max_bytes = max(self.total, self.max_bytes)
        self.max_messages = max(len(self.rows), self.max_messages)

    def report(self):
        return {"window_ns": self.width_ns, "max_bytes": self.max_bytes,
                "max_messages": self.max_messages}


def burst_stats(root, manifest):
    windows = [Window(100_000_000), Window(1_000_000_000)]
    count = total = largest = 0
    for item in manifest["chunks"]:
        with (root / item["file"]).open("rb") as stream:
            for line in stream:
                now = int(json.loads(line)["local_monotonic_ns"])
                size = len(line)
                for window in windows:
                    window.add(now, size)
                largest = max(largest, size)
                total += size
                count += 1
    need(count == MESSAGES and total == BYTES, "Q298_BURST_COUNTS")
    return {"max_frame_bytes": largest,
            "windows": [w.report() for w in windows]}


def stress_index(root, target, start):
    """Synthetic IDs only; separate database, no invented market evidence."""
    pairs = (target + 1) // 2
    index = TradeIndex(root / "synthetic-trades.db")
    try:
        for i in range(pairs):
            trade = {"product_id": PRODUCTS[i % 2], "trade_id": str(i),
                     "price": "100.123", "size": "0.001", "side": "BUY",
                     "time": "2026-01-01T00:00:00Z"}
            index.add("primary", trade)
            index.add("secondary", trade)
            if i % 4096 == 0:
                index.db.commit()
                check_limits(root, start)
        cross = index.compare()
        need(index.count == pairs * 2
             and sum(v["common_ids"] for v in cross.values()) == pairs
             and all(v["divergent_ids"] == 0 for v in cross.values()),
             "Q298_SYNTHETIC_INDEX_MISMATCH")
        # Verify the collision detection at high index occupancy.
        try:
            index.add("primary", {**trade, "size": "0.002"})
        except ValueError as exc:
            need(str(exc) == "TRADE_ID_CONFLICT_WITHIN_SOURCE",
                 "Q298_COLLISION_WRONG_REASON")
        else:
            raise ValueError("Q298_COLLISION_NOT_REJECTED")
        return {"synthetic_rows": index.count, "matching_pairs": pairs,
                "conflict_rejected": True,
                "db_bytes": (root / "synthetic-trades.db").stat().st_size}
    finally:
        index.close()


def run(zip_path, receipts, root, output):
    root.mkdir(mode=0o700)
    start = time.monotonic()
    result = {"schema": "q298_offline_capacity_v1", "status": "FAIL",
              "five_hour_approved": False, "market_socket_opened": False,
              "real_orders_enabled": False, "zero_gap_certified": False,
              "source_sha": os.environ.get("GITHUB_SHA", "LOCAL"),
              "run_id": int(os.environ.get("GITHUB_RUN_ID", "0")),
              "repeat_replay_is_not_new_market_data": True}
    try:
        q297 = audit(pin_file(receipts / "b5/b5.json", B5_JSON),
                     pin_file(receipts / "c2/c2.json", C2_JSON))
        free = shutil.disk_usage(root).free
        need(free >= admission(18000, 1), "Q298_HOST_DISK_PREFLIGHT")
        result["free_disk_at_start_bytes"] = free
        archive = root / "hour"
        manifest = extract_hour(zip_path, archive)
        result["historical_burst"] = burst_stats(archive, manifest)
        result["historical_manifest_sha256"] = MANIFEST
        # Cover both full-length independent sources at the 2x pilot estimate.
        per_source = max(
            (q297["projected_per_attempt_messages"] + MESSAGES - 1) // MESSAGES,
            (q297["projected_per_attempt_archive_bytes"] + BYTES - 1) // BYTES,
        )
        need(1 <= per_source <= 20, "Q298_REPEAT_BOUND")
        index = TradeIndex(root / "historical-trades.db")
        reference = None
        replay_start = time.monotonic()
        result["replay_passes"] = []
        try:
            for source in ("primary", "secondary"):
                for _ in range(per_source):
                    before = time.monotonic()
                    a = scan(archive, MANIFEST, source, index)
                    need(a["sealed_messages"] == MESSAGES
                         and a["sealed_bytes"] == BYTES
                         and a["sealed_chunks"] == CHUNKS
                         and a["unsealed_entries_excluded"] == 0
                         and len(a["epochs"]) == 1
                         and a["epochs"][0]["both_books_initialized_and_updated"],
                         "Q298_REPLAY_COUNTS")
                    books = a["epochs"][0]["book_sha256"]
                    if reference is None:
                        reference = books
                    need(books == reference, "Q298_REPLAY_NONDETERMINISM")
                    result["replay_passes"].append({
                        "source": source,
                        "seconds": round(time.monotonic() - before, 6),
                        "peak_rss_bytes": peak_rss(),
                    })
                    check_limits(root, start)
                    print("Q298_REPLAY_PASS", len(result["replay_passes"]),
                          flush=True)
            trades = index.compare()
            need(all(v["primary_ids"] == v["secondary_ids"]
                     == v["matching_ids"] > 0 and v["divergent_ids"] == 0
                     for v in trades.values()), "Q298_HISTORICAL_JOIN")
            result["historical_distinct_index_rows"] = index.count
        finally:
            index.close()
        result["replay_seconds"] = round(time.monotonic() - replay_start, 6)
        result["cumulative_replayed_messages"] = 2 * per_source * MESSAGES
        result["cumulative_replayed_bytes"] = 2 * per_source * BYTES
        result["unique_historical_messages"] = MESSAGES
        result["index_stress"] = stress_index(
            root, q297["projected_trade_index_rows"], start)
        check_limits(root, start)
        result["status"] = "PASS"
    except Exception as exc:
        result["failure"] = (str(exc) if isinstance(exc, ValueError)
                             and str(exc).startswith("Q298_")
                             else type(exc).__name__)
    result["peak_process_rss_bytes"] = peak_rss()
    result["elapsed_seconds"] = round(time.monotonic() - start, 6)
    result["free_disk_at_end_bytes"] = shutil.disk_usage(root).free
    with output.open("x") as stream:
        json.dump(result, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
    print("Q298_" + result["status"], result.get("failure", ""), flush=True)
    return result["status"] == "PASS"


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--q7", type=Path, required=True)
    p.add_argument("--receipts", type=Path, required=True)
    p.add_argument("--workdir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    raise SystemExit(0 if run(a.q7, a.receipts, a.workdir, a.output) else 2)
