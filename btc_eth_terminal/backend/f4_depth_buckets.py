"""Bounded historical full observed L2 receiver-time bucket projection.

One admitted connection per invocation; no forward-fill, no exchange clock or
execution claim. Last L2 observation per occupied bucket, not bucket-end state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from independent_soak_replay import replay
from v35_archive_ingress import verify_capture
from v23_causal_microstructure import read_l2

SCALE = 10**18
MAX_LEVELS = 200_000
MAX_BUCKETS = 2_000
MAX_PRICE_BINS = 500
MAX_WORK = 20_000_000
PRODUCTS = ("BTC-USD", "ETH-USD")


def units(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,18}(?:\.[0-9]{1,18})?", value):
        raise ValueError("F4_DECIMAL_DOMAIN")
    whole, _, frac = value.partition(".")
    return int(whole) * SCALE + int(frac.ljust(18, "0") or "0")


def decimal(value):
    whole, fraction = divmod(value, SCALE)
    return str(whole) + ("." + str(fraction).zfill(18).rstrip("0") if fraction else "")


def project(events, *, product, epoch, start_ms, end_ms, bucket_ms, low, high, width):
    if product not in PRODUCTS or not isinstance(epoch, str) or not re.fullmatch(r"[0-9a-f]{64}:0", epoch):
        raise ValueError("F4_EPOCH")
    if not all(type(x) is int for x in (start_ms, end_ms, bucket_ms)) or not (
        0 < start_ms < end_ms < 2**53 and 100 <= bucket_ms <= 60_000
        and start_ms % bucket_ms == 0 and end_ms % bucket_ms == 0
        and (end_ms-start_ms)//bucket_ms <= MAX_BUCKETS
    ):
        raise ValueError("F4_TIME_BOUNDS")
    lo, hi, step = map(units, (low, high, width))
    if not 0 < lo < hi or not step or lo % step or hi % step or (hi-lo)//step > MAX_PRICE_BINS:
        raise ValueError("F4_PRICE_GRID")
    book = {"bid": {}, "offer": {}}
    initialized = False
    prior_ms = prior_seq = None
    current_bucket = None
    last = None
    rows = []
    work = 0

    def emit():
        nonlocal work
        if last is None:
            return
        t, seq = last
        work += sum(len(side) for side in book.values())
        if work > MAX_WORK:
            raise ValueError("F4_AGGREGATION_BUDGET")
        if not book["bid"] or not book["offer"] or max(book["bid"]) >= min(book["offer"]):
            raise ValueError("F4_INVALID_BOOK")
        cells = {}
        for side, levels in book.items():
            for p, q in levels.items():
                if lo <= p < hi:
                    k = (side, (p-lo)//step)
                    cells[k] = cells.get(k, 0) + q
        rows.append({"bucket_start_ms": current_bucket, "observed_receiver_ms": t,
                     "source_sequence": seq, "best_bid": decimal(max(book["bid"])),
                     "best_ask": decimal(min(book["offer"])),
                     "cells": [{"side": s, "price_low": decimal(lo+i*step), "quantity": decimal(q)}
                               for (s, i), q in sorted(cells.items())]})

    for t, seq, p, kind, updates in events:
        if p != product:
            continue
        if type(t) is not int or type(seq) is not int or t <= 0 or seq < 0 or (
            prior_ms is not None and (t < prior_ms or seq < prior_seq or (seq == prior_seq and t != prior_ms))
        ):
            raise ValueError("F4_ORDER")
        prior_ms, prior_seq = t, seq
        if t >= end_ms:
            break
        bucket = t // bucket_ms * bucket_ms
        if current_bucket is not None and bucket != current_bucket:
            emit()
            current_bucket = None
            last = None
        if kind == "snapshot" and not initialized:
            initialized = True
        elif kind != "update" or not initialized:
            raise ValueError("F4_SNAPSHOT_BOUNDARY")
        for u in updates:
            side = u["side"]
            if side not in book:
                raise ValueError("F4_SIDE")
            px, qty = units(u["price_level"]), units(u["new_quantity"])
            if px <= 0:
                raise ValueError("F4_PRICE")
            if qty:
                book[side][px] = qty
            else:
                book[side].pop(px, None)
            if sum(len(x) for x in book.values()) > MAX_LEVELS:
                raise ValueError("F4_LEVEL_BUDGET")
        if t >= start_ms:
            current_bucket = bucket
            last = (t, seq)
    emit()
    observed = {r["bucket_start_ms"] for r in rows}
    return {"schema": "f4_observed_depth_buckets_v1", "mode": "ARCHIVE_REPLAY",
            "product": product, "epoch": epoch, "time_bucket_ms": bucket_ms,
            "price_low": decimal(lo), "price_high_exclusive": decimal(hi),
            "price_width": decimal(step), "definition": "LAST_L2_OBSERVATION_PER_OCCUPIED_RECEIVER_BUCKET",
            "coverage": "ALL_OBSERVED_L2_LEVELS_INSIDE_REQUESTED_PRICE_WINDOW",
            "missing_buckets_ms": [t for t in range(start_ms, end_ms, bucket_ms) if t not in observed],
            "rows": rows, "live_feed": False, "execution_authorized": False,
            "zero_gap_certified": False, "oos_approved": False}


def build(root, pin, **kwargs):
    root = Path(root)
    cert = verify_capture(root, pin)
    if not cert["source_eligible_for_state_replay"] or cert["connections"] != 1:
        raise ValueError("F4_REQUIRES_ADMITTED_SINGLE_EPOCH")
    manifest_bytes = (root / "manifest.json").read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != pin:
        raise ValueError("F4_MANIFEST_CHANGED")
    witness = replay(root)
    if witness["connections"] != 1 or witness["messages"] != cert["messages"]:
        raise ValueError("F4_INDEPENDENT_REPLAY_MISMATCH")
    manifest = json.loads(manifest_bytes)
    result = project(read_l2(root, manifest["chunks"]), epoch=pin+":0", **kwargs)
    if (root / "manifest.json").read_bytes() != manifest_bytes:
        raise ValueError("F4_MANIFEST_CHANGED")
    result["source_manifest_sha256"] = pin
    result["source_messages"] = cert["messages"]
    result["independent_replay_status"] = witness["status"]
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("archive", type=Path)
    p.add_argument("--manifest-sha256", required=True)
    p.add_argument("--product", choices=PRODUCTS, required=True)
    for name in ("start-ms", "end-ms", "bucket-ms"):
        p.add_argument("--"+name, type=int, required=True)
    for name in ("low", "high", "width"):
        p.add_argument("--"+name, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = vars(p.parse_args())
    archive, pin, output = (args.pop(k) for k in ("archive", "manifest_sha256", "output"))
    result = build(archive, pin, **args)
    with output.open("x") as stream:
        json.dump(result, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")


if __name__ == "__main__":
    main()
