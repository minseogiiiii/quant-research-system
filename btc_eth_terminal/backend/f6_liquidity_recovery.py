"""Frozen, long-only spot liquidity recovery diagnostic, event-driven.

A signal may see only earlier receiver-time packets. Hypothetical entry/exit
walk the first timely later book with frozen costs; never actual fills.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from decimal import Decimal, localcontext
from pathlib import Path
from statistics import median

from f4_depth_buckets import units
from f4_order_flow import PRODUCTS, read_packets
from f5_execution_depth import SCALE, walk
from f5_independent_clock import read_independent
from independent_soak_replay import replay, strict_json
from post_quote_markout import IndexedBook
from v35_archive_ingress import verify_capture

POLICY = {
    "step_ms": 1000,
    "warmup_samples": 10,
    "deplete_fraction": "0.5",
    "recover_fraction": "0.8",
    "hold_ms": 5000,
    "latency_ms": 100,
    "tolerance_ms": 1500,
    "max_age_ms": 3000,
    "capacity_fraction": "0.5",
    "fee_bps": "10",
    "extra_slippage_bps": "2",
    "quantity": {"BTC-USD": "0.01", "ETH-USD": "0.1"},
    "initial_cash": "10000",
}


def need(ok, reason):
    if not ok:
        raise ValueError("F6_RECOVERY_" + reason)


def digest(x):
    return hashlib.sha256(
        json.dumps(x, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def text(x):
    return format(x, "f")


class RecoverySignal:
    """Fixed grid, prior median L1 bid size; frozen baseline at depletion."""

    def __init__(self, policy):
        self.history = deque(maxlen=policy["warmup_samples"])
        self.anchor = None
        self.deplete = Decimal(policy["deplete_fraction"])
        self.recover = Decimal(policy["recover_fraction"])

    def reset(self):
        self.history.clear()
        self.anchor = None

    def step(self, bid_size, ask_size):
        signal = False
        if len(self.history) == self.history.maxlen:
            base = Decimal(median(self.history))
            if self.anchor is None and bid_size <= base * self.deplete:
                self.anchor = base
            elif self.anchor is not None and bid_size >= self.anchor * self.recover:
                signal = bid_size > ask_size
                self.anchor = None
        self.history.append(bid_size)
        return signal


def project(packets, product, policy=None, *, trace=None):
    p = json.loads(json.dumps(POLICY if policy is None else policy))
    need(product in PRODUCTS and set(p) == set(POLICY), "POLICY")
    step, warm, hold, lat, tol, age = (
        p[k]
        for k in (
            "step_ms",
            "warmup_samples",
            "hold_ms",
            "latency_ms",
            "tolerance_ms",
            "max_age_ms",
        )
    )
    need(
        all(type(x) is int for x in (step, warm, hold, lat, tol, age))
        and 100 <= step <= 60000
        and 2 <= warm <= 300
        and 100 <= hold <= 60000
        and 1 <= lat <= 60000
        and 0 <= tol <= 10000
        and 0 <= age <= 5000,
        "CLOCK",
    )
    deplete, recover = Decimal(p["deplete_fraction"]), Decimal(p["recover_fraction"])
    need(0 < deplete < recover <= 1, "THRESHOLDS")
    qty, cap, fee, slip = map(
        units,
        (
            p["quantity"][product],
            p["capacity_fraction"],
            p["fee_bps"],
            p["extra_slippage_bps"],
        ),
    )
    need(
        qty > 0
        and 0 < cap <= SCALE
        and 0 <= fee <= 100 * SCALE
        and 0 <= slip <= 500 * SCALE,
        "ECONOMICS",
    )
    cash = initial = Decimal(p["initial_cash"])
    need(cash > 0, "CASH")
    book = IndexedBook()
    signal = RecoverySignal(p)
    first = last = last_l2 = last_seq = decision = None
    prior = -1
    initialized = False
    pending = None
    position = None
    blocked = False
    outcomes = []
    signals = entries = exits = valid = missing = 0

    def mid():
        return Decimal(book.best("bid")[0] + book.best("offer")[0]) / (2 * SCALE)

    def record(kind, **row):
        if trace is not None:
            trace.append(dict(kind=kind, **row))

    def decide(t):
        nonlocal pending, signals, valid, missing
        fresh = (
            last_l2 is not None
            and last_l2 < t
            and t - last_l2 <= age
            and bool(book.bid)
            and bool(book.offer)
        )
        record(
            "GRID",
            time=t,
            cash=text(cash),
            base=text(Decimal(qty) / SCALE if position is not None else Decimal(0)),
            mid=text(mid()) if fresh else None,
        )
        if last_l2 is None or t - last_l2 > age or not book.bid or not book.offer:
            missing += 1
            signal.reset()
            return
        need(last_l2 < t, "FUTURE_FEATURE")
        valid += 1
        b = book.best("bid")
        a = book.best("offer")
        hit = signal.step(Decimal(b[1]), Decimal(a[1]))
        if hit:
            signals += 1
            if pending is None and position is None and not blocked:
                pending = ("BUY", t + lat, t, last_seq)
                record("SIGNAL", time=t, sequence=last_seq, mid=text(mid()))

    def reject(side, target, t, reason):
        nonlocal pending, blocked
        outcomes.append(
            {"side": side, "target_ms": target, "observed_ms": t, "status": reason}
        )
        record("REJECT", side=side, target_ms=target, time=t, status=reason)
        if side == "SELL":
            blocked = True
        pending = None

    with localcontext() as ctx:
        ctx.prec = 80
        for seq, t, wire in packets:
            need(
                type(seq) is int
                and seq == prior + 1
                and type(t) is int
                and t > 0
                and (last is None or t >= last),
                "ORDER",
            )
            prior = seq
            last = t
            if first is None:
                first = t
                decision = ((t + step - 1) // step) * step
            while decision <= t:
                need(valid + missing < 200000, "DECISION_BUDGET")
                decide(decision)
                decision += step
            if (
                position is not None
                and pending is not None
                and pending[0] == "SELL"
                and not position.get("exit_decision_recorded")
                and t >= pending[2]
            ):
                dt = pending[2]
                fresh = (
                    last_l2 is not None
                    and last_l2 < dt
                    and dt - last_l2 <= age
                    and bool(book.bid)
                    and bool(book.offer)
                )
                record(
                    "EXIT_DECISION",
                    time=dt,
                    sequence=last_seq if fresh else None,
                    mid=text(mid()) if fresh else None,
                )
                position["exit_decision_recorded"] = True
            affected = False
            if wire["channel"] == "l2_data":
                for e in wire["events"]:
                    if e["product_id"] != product:
                        continue
                    if e["type"] == "snapshot" and not initialized:
                        initialized = True
                    elif e["type"] != "update" or not initialized:
                        raise ValueError("F6_RECOVERY_EPOCH")
                    for u in e["updates"]:
                        px, q = units(u["price_level"]), units(u["new_quantity"])
                        need(px > 0 and u["side"] in ("bid", "offer"), "LEVEL")
                        book.change(u["side"], px, q)
                        need(len(book.bid) + len(book.offer) <= 200000, "DEPTH_BOUND")
                    affected = True
            if affected:
                need(
                    book.bid
                    and book.offer
                    and book.best("bid")[0] < book.best("offer")[0],
                    "BOOK",
                )
                last_l2, last_seq = t, seq
            if pending is None:
                continue
            side, target, decision_t, signal_seq = pending
            if t > target + tol:
                reject(side, target, t, "NO_TIMELY_BOOK")
                continue
            if t < target or not affected:
                continue
            r = walk(book.bid, book.offer, side, qty, cap, fee, slip)
            if r["status"] != "INDICATIVE_FULL_DEPTH_QUOTE":
                reject(side, target, t, r["status"])
                continue
            amount = Decimal(r["all_in_quote"])
            if side == "BUY":
                if amount > cash:
                    reject(side, target, t, "INSUFFICIENT_CASH")
                    continue
                record("BUY", time=t, sequence=seq, decision_ms=decision_t, quote=r)
                cash -= amount
                entries += 1
                position = {
                    "entry_quote": amount,
                    "entry_ms": t,
                    "entry_seq": seq,
                    "signal_ms": decision_t,
                    "signal_seq": signal_seq,
                }
                pending = ("SELL", t + hold + lat, t + hold, seq)
            else:
                need(position is not None, "SELL_WITHOUT_POSITION")
                record("SELL", time=t, sequence=seq, decision_ms=decision_t, quote=r)
                cash += amount
                exits += 1
                pnl = amount - position["entry_quote"]
                outcomes.append(
                    {
                        "status": "INDICATIVE_ROUNDTRIP",
                        "signal_ms": position["signal_ms"],
                        "signal_sequence": position["signal_seq"],
                        "entry_ms": position["entry_ms"],
                        "entry_sequence": position["entry_seq"],
                        "exit_ms": t,
                        "exit_sequence": seq,
                        "net_quote": text(pnl),
                        "net_bps": text(pnl / position["entry_quote"] * 10000),
                    }
                )
                position = None
                pending = None
        if pending is not None:
            reject(pending[0], pending[1], last, "CAPTURE_ENDED_BEFORE_TARGET")
        need(first is not None, "EMPTY")
        return {
            "schema": "f6_liquidity_recovery_diagnostic_v1",
            "product": product,
            "policy_sha256": digest(p),
            "policy": p,
            "source_first_ms": first,
            "source_last_ms": last,
            "source_messages": prior + 1,
            "valid_decisions": valid,
            "missing_decisions": missing,
            "signals": signals,
            "entries": entries,
            "closed_roundtrips": exits,
            "unresolved_position": position is not None,
            "outcomes": outcomes,
            "realized_net_quote": text(
                sum(
                    (
                        Decimal(o["net_quote"])
                        for o in outcomes
                        if o["status"] == "INDICATIVE_ROUNDTRIP"
                    ),
                    Decimal(0),
                )
            ),
            "final_cash_if_flat": text(cash) if position is None else None,
            "baseline_cash": text(initial),
            "actual_fills_verified": False,
            "oos_approved": False,
            "alpha_confirmed": False,
            "execution_authorized": False,
        }


def build(root, pin, product):
    root = Path(root)
    c = verify_capture(root, pin)
    need(c["source_eligible_for_state_replay"] and c["connections"] == 1, "ADMISSION")
    raw = (root / "manifest.json").read_bytes()
    need(hashlib.sha256(raw).hexdigest() == pin, "PIN")
    w = replay(root)
    need(w["messages"] == c["messages"] and w["connections"] == 1, "REPLAY")
    m = strict_json(raw)
    a = project(read_packets(root, m), product)
    b = project(read_independent(root, m), product)
    need(
        a == b
        and a["source_messages"] == c["messages"]
        and (root / "manifest.json").read_bytes() == raw,
        "PARSER_PARITY",
    )
    a.update(
        source_manifest_sha256=pin,
        epoch=pin + ":0",
        independent_input_parser_parity=True,
    )
    return a
