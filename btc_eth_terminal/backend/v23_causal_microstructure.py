"""v23 causal, receiver-time L2 feature diagnostic; NO alpha, labels, or orders.

Uses the immutable, previously audited one-hour Coinbase spot capture. Creates
preregistered 5-minute snapshots of top-of-book microprice, imbalance and a
rolling 5-minute L1 OFI. OFI is an *engineering definition*, not executable
alpha and is not a claim about an exchange-atomic order-event sequence.
"""

from __future__ import annotations

import argparse
import heapq
import json
from collections import deque
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from audit_soak import audit as admit_source
from v21_source_evidence import iso_ms

PRODUCTS = ("BTC-USD", "ETH-USD")
WINDOW_MS = 300000
STALENESS_MS = 5000


class FeatureReject(ValueError):
    pass


class Book:
    def __init__(self):
        self.level = {"bid": {}, "offer": {}}
        self.heap = {"bid": [], "offer": []}
        self.initialized = False

    def apply(self, kind, updates):
        if kind == "snapshot":
            if self.initialized:
                raise FeatureReject("Duplicate snapshot")
            self.initialized = True
        elif kind != "update" or not self.initialized:
            raise FeatureReject("Invalid type or missing snapshot")
        for u in updates:
            side = u["side"]
            if side not in self.level:
                raise FeatureReject("Invalid L2 side")
            p, q = Decimal(u["price_level"]), Decimal(u["new_quantity"])
            if not p.is_finite() or not q.is_finite() or p <= 0 or q < 0:
                raise FeatureReject("Invalid L2 price or size")
            if q == 0:
                self.level[side].pop(p, None)
            else:
                self.level[side][p] = q
                heapq.heappush(self.heap[side], -p if side == "bid" else p)

    def best(self):
        if not self.initialized:
            return None
        out = []
        for side in ("bid", "offer"):
            heap = self.heap[side]
            levels = self.level[side]
            while heap:
                p = -heap[0] if side == "bid" else heap[0]
                if p in levels:
                    break
                heapq.heappop(heap)
            if not heap:
                return None
            out.append((p, levels[p]))
        if out[0][0] >= out[1][0]:
            raise FeatureReject("Crossed or locked source book")
        return out[0], out[1]


def ofi_delta(old, new):
    """Classical L1 OFI (positive = upward buy pressure), Decimal units."""
    if old is None or new is None:
        return Decimal(0)
    (pb0, qb0), (pa0, qa0) = old
    (pb1, qb1), (pa1, qa1) = new
    return (
        (qb1 if pb1 >= pb0 else Decimal(0))
        - (qb0 if pb1 <= pb0 else Decimal(0))
        - (qa1 if pa1 <= pa0 else Decimal(0))
        + (qa0 if pa1 >= pa0 else Decimal(0))
    )


def features_from_events(events, decisions, max_age_ms=STALENESS_MS):
    """Accept *already audited* source order. Never fetch future events early."""
    if (
        not decisions
        or any(type(t) is not int for t in decisions)
        or decisions != sorted(set(decisions))
    ):
        raise FeatureReject("Invalid strictly increasing decision grid")
    if any(t < 1_500_000_000_000 for t in decisions):
        raise FeatureReject("Invalid UTC millisecond clock")
    if type(max_age_ms) is not int or max_age_ms < 0:
        raise FeatureReject("Invalid staleness policy")

    books = {p: Book() for p in PRODUCTS}
    history = {p: deque() for p in PRODUCTS}
    rolling = {p: Decimal(0) for p in PRODUCTS}
    last_l2 = {p: None for p in PRODUCTS}
    last_seq = {p: None for p in PRODUCTS}
    last_ts, prev_seq = None, None
    output = []
    decisions_iter = iter(decisions)
    decision = next(decisions_iter, None)

    def emit(t):
        for product in PRODUCTS:
            entries = history[product]
            while entries and entries[0][0] <= t - WINDOW_MS:
                _, value = entries.popleft()
                rolling[product] -= value
            state = books[product].best()
            age = None if last_l2[product] is None else t - last_l2[product]
            if age is not None and age < 0:
                raise FeatureReject("Future event at earlier decision")
            valid = state is not None and age is not None and age <= max_age_ms
            row = {
                "instrument": product,
                "decision_receive_utc_ms": t,
                "status": "DIAGNOSTIC_CAUSAL_L1" if valid else "NO_ADMISSIBLE_QUOTE",
                "source_message_sequence": last_seq[product],
                "feature_max_received_utc_ms": last_l2[product],
                "source_age_ms": age,
                "point_in_time_is_receiver_only": True,
                "exchange_clock_calibrated": False,
                "original_feature_label_pit_verified": False,
                "eligible_for_oos_or_trading": False,
            }
            if valid:
                (bid, qb), (ask, qa) = state
                denom = qb + qa
                if denom <= 0:
                    raise FeatureReject("No displayed liquidity")
                row.update(
                    {
                        "best_bid": str(bid),
                        "best_ask": str(ask),
                        "best_bid_qty": str(qb),
                        "best_ask_qty": str(qa),
                        "microprice": str((ask * qb + bid * qa) / denom),
                        "best_imbalance": str((qb - qa) / denom),
                        "ofi_rolling_5m_base": str(rolling[product]),
                        "l2_update_events_rolling_5m": len(entries),
                    }
                )
            output.append(row)

    for received, seq, product, kind, updates in events:
        if (
            type(received) is not int
            or type(seq) is not int
            or product not in books
            or (last_ts is not None and received < last_ts)
            or (
                prev_seq is not None
                and (seq < prev_seq or (seq == prev_seq and received != last_ts))
            )
        ):
            raise FeatureReject("Invalid event chronology")
        while decision is not None and decision < received:
            emit(decision)
            decision = next(decisions_iter, None)
        book = books[product]
        previous = book.best()
        book.apply(kind, updates)
        current = book.best()
        if kind == "update":
            d = ofi_delta(previous, current)
            history[product].append((received, d))
            rolling[product] += d
        last_l2[product] = received
        last_seq[product] = seq
        last_ts, prev_seq = received, seq
    while decision is not None:
        emit(decision)
        decision = next(decisions_iter, None)
    return output


def read_l2(root, chunks):
    prev_seq = None
    for chunk in chunks:
        path = root / chunk["file"]
        if path.is_symlink() or not path.is_file():
            raise FeatureReject("Missing/unsafe archived chunk")
        raw = path.read_bytes()
        if sha256(raw).hexdigest() != chunk["archive_sha256"]:
            raise FeatureReject("Original source chunk changed")
        for line in raw.splitlines():
            envelope = json.loads(line)
            msg = json.loads(envelope["raw_text"])
            seq = msg["sequence_num"]
            if prev_seq is not None and seq != prev_seq + 1:
                raise FeatureReject("Source message sequence changed")
            prev_seq = seq
            if msg["channel"] != "l2_data":
                continue
            received = iso_ms(envelope["received_utc"])
            for ev in msg["events"]:
                yield (received, seq, ev["product_id"], ev["type"], ev["updates"])


def create_report(root: Path, v21_reference: dict):
    root = Path(root)
    admitted = admit_source(root)
    if (
        admitted["status"] != "INTERNALLY_REPLAYED_NO_KNOWN_GAP"
        or admitted["connection_count"] != 1
    ):
        raise FeatureReject("Unadmitted source or disconnected collection")
    manifest_bytes = (root / "manifest.json").read_bytes()
    digest = sha256(manifest_bytes).hexdigest()
    if digest != v21_reference["source_manifest_sha256"]:
        raise FeatureReject("Reference report source differs")
    m = json.loads(manifest_bytes)
    first = ((iso_ms(m["sampled_from_utc"]) + WINDOW_MS - 1) // WINDOW_MS) * WINDOW_MS
    last = iso_ms(m["finished_at_utc"])
    decisions = list(range(first, last + 1, WINDOW_MS))
    rows = features_from_events(read_l2(root, m["chunks"]), decisions)
    expected = v21_reference["rows"]
    if len(expected) != len(rows):
        raise FeatureReject("v21 fixed decisions not preserved")
    for r, b in zip(rows, expected):
        if (
            r["instrument"] != b["instrument"]
            or r["decision_receive_utc_ms"] != b["decision_receive_utc_ms"]
            or r["source_message_sequence"] != b["source_message_sequence"]
            or r["feature_max_received_utc_ms"] != b["last_l2_received_utc_ms"]
            or (
                (r["status"] == "DIAGNOSTIC_CAUSAL_L1")
                != (b["status"] == "RECEIVER_ASOF_L2_QUOTE")
            )
        ):
            raise FeatureReject("Independent v21 receiver decision mismatch")
        if r["status"] == "DIAGNOSTIC_CAUSAL_L1":
            for a, c in (
                ("best_bid", "best_bid"),
                ("best_ask", "best_ask"),
                ("best_bid_qty", "displayed_best_bid_qty"),
                ("best_ask_qty", "displayed_best_ask_qty"),
            ):
                if Decimal(r[a]) != Decimal(b[c]):
                    raise FeatureReject("v21 quote/quantity parity failure")
    if (root / "manifest.json").read_bytes() != manifest_bytes:
        raise FeatureReject("Source manifest changed during analysis")
    return {
        "status": "REAL_SOURCE_CAUSAL_FEATURE_DIAGNOSTIC_NOT_OOS",
        "source_manifest_sha256": digest,
        "source_message_count": admitted["message_count"],
        "input_reference": "V21_SOURCE_EVIDENCE_REPORT.json",
        "sample_grid": "UTC five-minute receiver-asof, 5min rolling OFI, no lookahead",
        "ofi_definition": "L1 bid/ask price-change signed displayed-size delta; snapshot not counted",
        "original_user_feature_label_pair_verified": False,
        "eligible_for_oos_or_execution": False,
        "results": rows,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", type=Path)
    ap.add_argument("--reference", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    report = create_report(args.source, json.loads(args.reference.read_text()))
    args.output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    print(report["status"], "rows", len(report["results"]))


if __name__ == "__main__":
    main()
