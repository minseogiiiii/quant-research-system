"""Receiver-time L1 OFI and separate future midquote labels; no alpha or fills.

Features use packets strictly before decision_ms, with a full observed warm-up.
Labels use first later same-product L2 packet at/after a frozen horizon within
tolerance. Each invocation accepts a single verified archive epoch only.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
from collections import deque
from decimal import Decimal, localcontext
from pathlib import Path

from f4_depth_buckets import units
from f4_order_flow import PRODUCTS, read_packets
from independent_soak_replay import replay, strict_json
from post_quote_markout import IndexedBook
from v35_archive_ingress import verify_capture

MAX_DECISIONS = 2000
MAX_LEVELS = 200000
MAX_EVENTS = 1000000


def need(ok, reason):
    if not ok:
        raise ValueError(reason)


def fmt(x):
    return format(x, "f")


def ofi(old, new):
    if old is None or new is None:
        return 0
    (pb, qb), (pa, qa) = old
    (p, q), (a, v) = new
    return (
        (q if p >= pb else 0)
        - (qb if p <= pb else 0)
        - (v if a <= pa else 0)
        + (qa if a >= pa else 0)
    )


def project(
    packets,
    *,
    product,
    start_ms,
    end_ms,
    step_ms,
    window_ms,
    max_age_ms,
    horizons_ms,
    tolerance_ms,
):
    need(
        product in PRODUCTS
        and all(
            type(x) is int
            for x in (start_ms, end_ms, step_ms, window_ms, max_age_ms, tolerance_ms)
        ),
        "OFI_POLICY",
    )
    need(
        0 < start_ms < end_ms < 2**53 - 120000
        and 100 <= step_ms <= 60000
        and start_ms % step_ms == end_ms % step_ms == 0
        and 100 <= window_ms <= 300000
        and 0 <= max_age_ms <= 5000
        and 0 <= tolerance_ms <= 10000
        and (end_ms - start_ms) // step_ms <= MAX_DECISIONS,
        "OFI_GRID",
    )
    need(
        isinstance(horizons_ms, (list, tuple))
        and 1 <= len(horizons_ms) <= 6
        and all(type(x) is int and 100 <= x <= 60000 for x in horizons_ms)
        and list(horizons_ms) == sorted(set(horizons_ms)),
        "OFI_HORIZONS",
    )
    book = IndexedBook()
    initialized = False
    history = deque()
    rolling = 0
    prev_seq = -1
    first = last = snapshot_ms = last_l2 = last_seq = None
    features, labels, pending = [], [], []
    decision = start_ms
    updates = 0

    def state():
        b, a = book.best("bid"), book.best("offer")
        if b is None or a is None:
            return None
        need(b[0] < a[0], "OFI_CROSSED")
        return b, a

    def emit(t):
        nonlocal rolling
        while history and history[0][0] < t - window_ms:
            rolling -= history.popleft()[1]
        current = state()
        reason = None
        if (
            first is None
            or snapshot_ms is None
            or t - window_ms < max(first, snapshot_ms)
        ):
            reason = "INSUFFICIENT_OBSERVED_WARMUP"
        elif current is None or last_l2 is None or t - last_l2 > max_age_ms:
            reason = "NO_FRESH_QUOTE"
        row = {
            "decision_ms": t,
            "status": reason or "CAUSAL_FEATURE",
            "feature_available_ms": last_l2 if reason is None else None,
            "source_sequence": last_seq if reason is None else None,
        }
        features.append(row)
        if reason is not None:
            return
        need(last_l2 < t, "OFI_FEATURE_FUTURE")
        (b, q), (a, v) = current
        with localcontext() as ctx:
            ctx.prec = 80
            scale = Decimal(10**18)
            row.update(
                best_bid=fmt(Decimal(b) / scale),
                best_ask=fmt(Decimal(a) / scale),
                mid=fmt(Decimal(b + a) / (2 * scale)),
                ofi_base=fmt(Decimal(rolling) / scale),
                imbalance=fmt(Decimal(q - v) / Decimal(q + v)),
                microprice=fmt(Decimal(a * q + b * v) / (Decimal(q + v) * scale)),
                update_packets=len(history),
                source_age_ms=t - last_l2,
            )
        for h in horizons_ms:
            idx = len(labels)
            target = t + h
            labels.append(
                {
                    "decision_ms": t,
                    "horizon_ms": h,
                    "target_ms": target,
                    "status": "NO_FUTURE_OBSERVATION",
                    "label_available_ms": None,
                    "label_source_sequence": None,
                    "midquote_return_bps": None,
                }
            )
            heapq.heappush(pending, (target, idx, b + a))

    for seq, t, wire in packets:
        need(
            type(seq) is int
            and seq == prev_seq + 1
            and type(t) is int
            and 0 < t < 2**53
            and (last is None or t >= last),
            "OFI_PACKET_ORDER",
        )
        prev_seq = seq
        first = t if first is None else first
        while decision < end_ms and decision <= t:
            emit(decision)
            decision += step_ms
        last = t
        if wire["channel"] != "l2_data":
            continue
        affected = [e for e in wire["events"] if e["product_id"] == product]
        if not affected:
            continue
        before = state()
        had_update = False
        for event in affected:
            kind = event["type"]
            if kind == "snapshot" and not initialized:
                initialized = True
                snapshot_ms = t
            elif kind == "update" and initialized:
                had_update = True
            else:
                raise ValueError("OFI_SNAPSHOT_BOUNDARY")
            for u in event["updates"]:
                p, q = units(u["price_level"]), units(u["new_quantity"])
                need(p > 0 and u["side"] in ("bid", "offer"), "OFI_LEVEL")
                book.change(u["side"], p, q)
                need(len(book.bid) + len(book.offer) <= MAX_LEVELS, "OFI_LEVEL_BOUND")
        after = state()
        need(after is not None, "OFI_INCOMPLETE_BOOK")
        last_l2, last_seq = t, seq
        if had_update:
            delta = ofi(before, after)
            history.append((t, delta))
            rolling += delta
            updates += 1
            need(updates <= MAX_EVENTS, "OFI_EVENT_BOUND")
        # Remove old observations even if the next decision is far away.
        while history and history[0][0] < t - window_ms:
            rolling -= history.popleft()[1]
        while pending and pending[0][0] <= t:
            target, i, mid2 = heapq.heappop(pending)
            label = labels[i]
            if t - target > tolerance_ms:
                label["status"] = "FIRST_L2_OUTSIDE_TOLERANCE"
                continue
            (b, _), (a, _) = after
            with localcontext() as ctx:
                ctx.prec = 80
                value = Decimal((b + a - mid2) * 10000) / Decimal(mid2)
            label.update(
                status="OBSERVED_FUTURE_LABEL",
                label_available_ms=t,
                label_source_sequence=seq,
                midquote_return_bps=fmt(value),
            )
    need(first is not None, "OFI_EMPTY_SOURCE")
    while decision < end_ms:
        features.append(
            {
                "decision_ms": decision,
                "status": "OUTSIDE_OBSERVED_SOURCE",
                "feature_available_ms": None,
                "source_sequence": None,
            }
        )
        decision += step_ms
    return {
        "schema": "f4_causal_ofi_markout_v1",
        "mode": "ARCHIVE_REPLAY",
        "product": product,
        "policy": {
            "start_ms": start_ms,
            "end_ms": end_ms,
            "step_ms": step_ms,
            "window_ms": window_ms,
            "max_age_ms": max_age_ms,
            "horizons_ms": list(horizons_ms),
            "tolerance_ms": tolerance_ms,
            "feature_boundary": "STRICTLY_BEFORE_DECISION_MS",
            "ofi_definition": "L1_PACKET_FINAL_STATE_TRANSITION_BASE_UNITS",
            "label_definition": (
                "FIRST_SAME_PRODUCT_L2_AT_OR_AFTER_TARGET_WITHIN_TOLERANCE"
            ),
        },
        "source_first_receiver_ms": first,
        "source_last_receiver_ms": last,
        "features": features,
        "future_labels": labels,
        "live_feed": False,
        "execution_authorized": False,
        "zero_gap_certified": False,
        "oos_approved": False,
        "alpha_confirmed": False,
        "exchange_clock_calibrated": False,
    }


def build(root, pin, **kwargs):
    root = Path(root)
    cert = verify_capture(root, pin)
    need(
        cert["source_eligible_for_state_replay"] and cert["connections"] == 1,
        "OFI_SINGLE_ADMITTED_EPOCH",
    )
    raw = (root / "manifest.json").read_bytes()
    need(hashlib.sha256(raw).hexdigest() == pin, "OFI_MANIFEST_CHANGED")
    witness = replay(root)
    need(
        witness["connections"] == 1 and witness["messages"] == cert["messages"],
        "OFI_WITNESS_MISMATCH",
    )
    result = project(read_packets(root, strict_json(raw)), **kwargs)
    need((root / "manifest.json").read_bytes() == raw, "OFI_MANIFEST_CHANGED")
    result.update(
        source_manifest_sha256=pin,
        epoch=pin + ":0",
        source_messages=cert["messages"],
        independent_replay_status=witness["status"],
    )
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("archive", type=Path)
    p.add_argument("--manifest-sha256", required=True)
    p.add_argument("--product", choices=PRODUCTS, required=True)
    for name in (
        "start-ms",
        "end-ms",
        "step-ms",
        "window-ms",
        "max-age-ms",
        "tolerance-ms",
    ):
        p.add_argument("--" + name, type=int, required=True)
    p.add_argument("--horizons-ms", type=int, nargs="+", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = vars(p.parse_args())
    root, pin, out = (args.pop(k) for k in ("archive", "manifest_sha256", "output"))
    result = build(root, pin, **args)
    with out.open("x") as stream:
        json.dump(result, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")


if __name__ == "__main__":
    main()
