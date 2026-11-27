"""Hypothetical full displayed-depth walk, not a fill or execution simulator.

Explicit quantities, capacity haircut, extra adverse price adjustment and fee.
First same-product L2 observation at/after a frozen receiver-time target; no
later quote substitution on insufficient depth. No orders or strategy PnL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal, localcontext
from pathlib import Path

from f4_depth_buckets import units
from f4_order_flow import PRODUCTS, read_packets
from independent_soak_replay import replay, strict_json
from post_quote_markout import IndexedBook
from v35_archive_ingress import verify_capture

SCALE = 10**18
MAX_LEVELS = 200000
MAX_INTENTS = 32


def need(ok, reason):
    if not ok:
        raise ValueError("F5_EXEC_" + reason)


def format_decimal(x):
    return format(x, "f")


def walk(bids, asks, side, quantity, capacity, fee, slip):
    """Input exact integer prices/sizes; capacity is fixed-point fraction."""
    need(
        side in ("BUY", "SELL")
        and quantity > 0
        and 0 < capacity <= SCALE
        and 0 <= fee <= 100 * SCALE
        and 0 <= slip <= 500 * SCALE,
        "WALK_DOMAIN",
    )
    need(bids and asks and max(bids) < min(asks), "BOOK")
    best_bid, best_ask = max(bids), min(asks)
    table = asks if side == "BUY" else bids
    remaining = quantity
    notional = 0
    levels_used = 0
    for price in sorted(table, reverse=side == "SELL"):
        available = table[price] * capacity // SCALE
        take = min(remaining, available)
        if take:
            notional += price * take
            remaining -= take
            levels_used += 1
        if remaining == 0:
            break
    if remaining:
        return {
            "status": "INSUFFICIENT_DISPLAYED_DEPTH",
            "levels_used": levels_used,
            "requested_base": format_decimal(Decimal(quantity) / SCALE),
            "available_base_at_haircut": format_decimal(
                Decimal(quantity - remaining) / SCALE
            ),
            "vwap": None,
            "all_in_quote": None,
            "mid_cost_bps": None,
        }
    with localcontext() as ctx:
        ctx.prec = 80
        qty = Decimal(quantity) / SCALE
        vwap = Decimal(notional) / (Decimal(quantity) * SCALE)
        mid = Decimal(best_bid + best_ask) / (2 * SCALE)
        touch = Decimal(best_ask if side == "BUY" else best_bid) / SCALE
        direction = 1 if side == "BUY" else -1
        gross = vwap * qty
        adjusted = gross * (1 + direction * Decimal(slip) / (SCALE * 10000))
        charge = adjusted * Decimal(fee) / (SCALE * 10000)
        net = adjusted + direction * charge
        return {
            "status": "INDICATIVE_FULL_DEPTH_QUOTE",
            "levels_used": levels_used,
            "requested_base": format_decimal(qty),
            "vwap": format_decimal(vwap),
            "mid": format_decimal(mid),
            "touch": format_decimal(touch),
            "gross_quote": format_decimal(gross),
            "adverse_adjusted_quote": format_decimal(adjusted),
            "assumed_fee_quote": format_decimal(charge),
            "all_in_quote": format_decimal(net),
            "depth_walk_bps": format_decimal(
                direction * (vwap - touch) / touch * 10000
            ),
            "mid_cost_bps": format_decimal(direction * (net / qty - mid) / mid * 10000),
        }


def validate(intents, policy):
    need(type(intents) is list and 1 <= len(intents) <= MAX_INTENTS, "INTENT_COUNT")
    need(
        set(policy)
        == {
            "latency_ms",
            "tolerance_ms",
            "max_signal_age_ms",
            "capacity_fraction",
            "fee_bps",
            "extra_slippage_bps",
        },
        "POLICY",
    )
    latency, tol, age = (
        policy[k] for k in ("latency_ms", "tolerance_ms", "max_signal_age_ms")
    )
    need(
        all(type(x) is int for x in (latency, tol, age))
        and 1 <= latency <= 60000
        and 0 <= tol <= 10000
        and 0 <= age <= 5000,
        "CLOCK_POLICY",
    )
    cap, fee, slip = (
        units(policy[k]) for k in ("capacity_fraction", "fee_bps", "extra_slippage_bps")
    )
    need(
        0 < cap <= SCALE and 0 <= fee <= 100 * SCALE and 0 <= slip <= 500 * SCALE,
        "ECONOMICS",
    )
    ids = set()
    out = []
    for i in intents:
        need(
            type(i) is dict
            and set(i) == {"id", "product", "side", "decision_ms", "quantity"},
            "INTENT",
        )
        key = i["id"]
        t = i["decision_ms"]
        need(
            isinstance(key, str)
            and 0 < len(key) <= 64
            and key not in ids
            and i["product"] in PRODUCTS
            and i["side"] in ("BUY", "SELL")
            and type(t) is int
            and 0 < t < 2**53 - 120000,
            "INTENT_DOMAIN",
        )
        qty = units(i["quantity"])
        need(0 < qty <= 1000000 * SCALE, "QUANTITY")
        ids.add(key)
        out.append((t, key, i, qty))
    return sorted(out), cap, fee, slip


def project(packets, intents, policy):
    ordered, cap, fee, slip = validate(intents, policy)
    books = {p: IndexedBook() for p in PRODUCTS}
    initialized = set()
    last_l2 = {}
    last_seq = {}
    pending = []
    results = {}
    pos = 0
    prior = -1
    last = None

    def reject(i, status, **extra):
        results[i["id"]] = {
            "id": i["id"],
            "product": i["product"],
            "side": i["side"],
            "decision_ms": i["decision_ms"],
            "status": status,
            "vwap": None,
            "all_in_quote": None,
            "mid_cost_bps": None,
            **extra,
        }

    def decide(item):
        t, _, i, qty = item
        p = i["product"]
        b = books[p]
        if (
            p not in initialized
            or not b.bid
            or not b.offer
            or last_l2[p] >= t
            or t - last_l2[p] > policy["max_signal_age_ms"]
        ):
            reject(i, "NO_FRESH_SIGNAL_BOOK")
        else:
            pending.append(
                (
                    t + policy["latency_ms"],
                    i,
                    qty,
                    last_l2[p],
                    last_seq[p],
                    max(b.bid) + min(b.offer),
                )
            )

    for seq, t, wire in packets:
        need(
            type(seq) is int
            and seq == prior + 1
            and type(t) is int
            and t > 0
            and (last is None or t >= last),
            "PACKET_ORDER",
        )
        prior = seq
        last = t
        while pos < len(ordered) and ordered[pos][0] <= t:
            decide(ordered[pos])
            pos += 1
        affected = set()
        if wire["channel"] == "l2_data":
            for e in wire["events"]:
                p = e["product_id"]
                need(p in books, "PRODUCT")
                if e["type"] == "snapshot" and p not in initialized:
                    initialized.add(p)
                elif e["type"] != "update" or p not in initialized:
                    raise ValueError("F5_EXEC_EPOCH")
                b = books[p]
                for u in e["updates"]:
                    px, q = units(u["price_level"]), units(u["new_quantity"])
                    need(px > 0 and u["side"] in ("bid", "offer"), "LEVEL")
                    b.change(u["side"], px, q)
                    need(len(b.bid) + len(b.offer) <= MAX_LEVELS, "LEVEL_BOUND")
                affected.add(p)
            for p in affected:
                b = books[p]
                need(
                    b.bid and b.offer and b.best("bid")[0] < b.best("offer")[0],
                    "INVALID_BOOK",
                )
                last_l2[p] = t
                last_seq[p] = seq
        waiting = []
        for target, i, qty, signal_time, signal_seq, signal_mid2 in pending:
            if t < target:
                waiting.append((target, i, qty, signal_time, signal_seq, signal_mid2))
                continue
            if t > target + policy["tolerance_ms"]:
                reject(
                    i,
                    "NO_TIMELY_TARGET_QUOTE",
                    signal_receiver_ms=signal_time,
                    signal_source_sequence=signal_seq,
                    target_ms=target,
                )
                continue
            if i["product"] not in affected:
                waiting.append((target, i, qty, signal_time, signal_seq, signal_mid2))
                continue
            b = books[i["product"]]
            r = walk(b.bid, b.offer, i["side"], qty, cap, fee, slip)
            if r["status"] == "INDICATIVE_FULL_DEPTH_QUOTE":
                with localcontext() as ctx:
                    ctx.prec = 80
                    direction = 1 if i["side"] == "BUY" else -1
                    r["decision_mid_cost_bps"] = format_decimal(
                        direction
                        * (
                            Decimal(r["all_in_quote"])
                            * SCALE
                            / Decimal(qty)
                            / (Decimal(signal_mid2) / (2 * SCALE))
                            - 1
                        )
                        * 10000
                    )
            results[i["id"]] = {
                "id": i["id"],
                "product": i["product"],
                "side": i["side"],
                "decision_ms": i["decision_ms"],
                "signal_receiver_ms": signal_time,
                "signal_source_sequence": signal_seq,
                "target_ms": target,
                "quote_receiver_ms": t,
                "quote_source_sequence": seq,
                **r,
            }
        pending = waiting
    for target, i, _qty, signal_time, signal_seq, _ in pending:
        reject(
            i,
            "NO_TIMELY_TARGET_QUOTE",
            signal_receiver_ms=signal_time,
            signal_source_sequence=signal_seq,
            target_ms=target,
        )
    for _, _, i, _ in ordered[pos:]:
        reject(i, "OUTSIDE_OBSERVED_SOURCE")
    return {
        "schema": "f5_indicative_depth_walk_v1",
        "mode": "ARCHIVE_REPLAY",
        "policy": policy,
        "intents_sha256": hashlib.sha256(
            json.dumps(intents, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "results": [results[i["id"]] for i in intents],
        "actual_fills_verified": False,
        "execution_authorized": False,
        "market_impact_verified": False,
        "oos_approved": False,
        "alpha_confirmed": False,
        "live_feed": False,
    }


def build(root, pin, intents, policy):
    validate(intents, policy)
    root = Path(root)
    cert = verify_capture(root, pin)
    need(
        cert["source_eligible_for_state_replay"] and cert["connections"] == 1,
        "SINGLE_EPOCH",
    )
    raw = (root / "manifest.json").read_bytes()
    need(hashlib.sha256(raw).hexdigest() == pin, "PIN")
    w = replay(root)
    need(w["messages"] == cert["messages"] and w["connections"] == 1, "WITNESS")
    result = project(read_packets(root, strict_json(raw)), intents, policy)
    need((root / "manifest.json").read_bytes() == raw, "MANIFEST_CHANGED")
    result.update(
        source_manifest_sha256=pin, epoch=pin + ":0", source_messages=cert["messages"]
    )
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("archive", type=Path)
    p.add_argument("--manifest-sha256", required=True)
    p.add_argument("--intents", type=Path, required=True)
    p.add_argument("--policy", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    result = build(
        a.archive,
        a.manifest_sha256,
        strict_json(a.intents.read_bytes()),
        strict_json(a.policy.read_bytes()),
    )
    with a.output.open("x") as f:
        json.dump(result, f, sort_keys=True, allow_nan=False)
        f.write("\n")


if __name__ == "__main__":
    main()
