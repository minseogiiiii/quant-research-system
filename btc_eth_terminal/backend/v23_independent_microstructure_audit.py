"""Independently reconstruct v23 L1 OFI from source using sorted price arrays.

Separate implementation from v23_causal_microstructure: bisect price-index,
independent rolling OFI arithmetic, independent raw archive traversal. The
market source is still the SAME original Coinbase capture, not independent
venue evidence, broker fills, or external clock attestation.
"""

from __future__ import annotations

import argparse
import json
from bisect import bisect_left, insort
from collections import deque
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path


class IndependentOFIReject(ValueError):
    pass


class SortedL2:
    def __init__(self):
        self.ladders = {"bid": [], "offer": []}
        self.amounts = {"bid": {}, "offer": {}}
        self.ready = False

    def set_levels(self, kind, updates):
        if kind == "snapshot":
            if self.ready:
                raise IndependentOFIReject("Repeated snapshot")
            self.ready = True
        elif kind != "update" or not self.ready:
            raise IndependentOFIReject("Update before valid snapshot")
        for u in updates:
            s = u["side"]
            if s not in self.amounts:
                raise IndependentOFIReject("Bad side")
            px, qty = Decimal(u["price_level"]), Decimal(u["new_quantity"])
            if not px.is_finite() or not qty.is_finite() or px <= 0 or qty < 0:
                raise IndependentOFIReject("Bad level")
            book, ladder = self.amounts[s], self.ladders[s]
            i = bisect_left(ladder, px)
            in_list = i < len(ladder) and ladder[i] == px
            if qty == 0:
                book.pop(px, None)
                if in_list:
                    ladder.pop(i)
            else:
                book[px] = qty
                if not in_list:
                    insort(ladder, px)

    def top(self):
        if not self.ready or not self.ladders["bid"] or not self.ladders["offer"]:
            return None
        bid, ask = self.ladders["bid"][-1], self.ladders["offer"][0]
        if bid >= ask:
            raise IndependentOFIReject("Crossed source book")
        return bid, self.amounts["bid"][bid], ask, self.amounts["offer"][ask]


def independent_delta(before, after):
    if before is None or after is None:
        return Decimal(0)
    bp, bq, ap, aq = before
    np, nq, mp, mq = after
    bid_part = nq - bq if np == bp else (nq if np > bp else -bq)
    ask_part = aq - mq if mp == ap else (-mq if mp < ap else aq)
    return bid_part + ask_part


def independent_verify(source_root: Path, report: dict):
    root = Path(source_root)
    manifest_path = root / "manifest.json"
    mb = manifest_path.read_bytes()
    if sha256(mb).hexdigest() != report["source_manifest_sha256"]:
        raise IndependentOFIReject("Source manifest identity mismatch")
    manifest = json.loads(mb)
    expected = report["results"]
    if len(expected) != 24:
        raise IndependentOFIReject("Unexpected fixed sample count")
    times = sorted(set(r["decision_receive_utc_ms"] for r in expected))
    if len(times) != 12 or len({r["instrument"] for r in expected}) != 2:
        raise IndependentOFIReject("Unexpected decision grid")
    src_seq = None
    last_received = None
    last_l2 = {"BTC-USD": None, "ETH-USD": None}
    last_seq = {"BTC-USD": None, "ETH-USD": None}
    books = {p: SortedL2() for p in last_l2}
    windows = {p: deque() for p in last_l2}
    totals = {p: Decimal(0) for p in last_l2}
    i = 0
    compares = 0

    def check(decision):
        nonlocal compares
        for product in ("BTC-USD", "ETH-USD"):
            q = windows[product]
            while q and q[0][0] <= decision - 300000:
                _, v = q.popleft()
                totals[product] -= v
            target = expected[compares]
            compares += 1
            if (
                target["instrument"] != product
                or target["decision_receive_utc_ms"] != decision
            ):
                raise IndependentOFIReject("Unexpected instrument/clock order")
            top = books[product].top()
            stamp = last_l2[product]
            age = None if stamp is None else decision - stamp
            valid = top is not None and age is not None and 0 <= age <= 5000
            if (target["status"] == "DIAGNOSTIC_CAUSAL_L1") != valid:
                raise IndependentOFIReject("As-of eligibility mismatch")
            if (
                target["feature_max_received_utc_ms"] != stamp
                or target["source_message_sequence"] != last_seq[product]
            ):
                raise IndependentOFIReject("Source receive clock or sequence mismatch")
            if valid:
                bp, bq, ap, aq = top
                checks = {
                    "best_bid": bp,
                    "best_bid_qty": bq,
                    "best_ask": ap,
                    "best_ask_qty": aq,
                    "ofi_rolling_5m_base": totals[product],
                    "best_imbalance": (bq - aq) / (bq + aq),
                    "microprice": (bp * aq + ap * bq) / (bq + aq),
                }
                for key, value in checks.items():
                    if Decimal(target[key]) != value:
                        raise IndependentOFIReject(f"Independent {key} mismatch")
                if target["l2_update_events_rolling_5m"] != len(q):
                    raise IndependentOFIReject("Event count mismatch")

    for chunk in manifest["chunks"]:
        path = root / chunk["file"]
        if path.is_symlink():
            raise IndependentOFIReject("Unsafe original chunk")
        data = path.read_bytes()
        if sha256(data).hexdigest() != chunk["archive_sha256"]:
            raise IndependentOFIReject("Mutated source chunk")
        for rawline in data.splitlines():
            rec = json.loads(rawline)
            msg = json.loads(rec["raw_text"])
            n = msg["sequence_num"]
            if src_seq is not None and n != src_seq + 1:
                raise IndependentOFIReject("Original stream sequence gap")
            src_seq = n
            if msg["channel"] != "l2_data":
                continue
            recv = int(
                datetime.fromisoformat(
                    rec["received_utc"].replace("Z", "+00:00")
                ).timestamp()
                * 1000
            )
            if last_received is not None and recv < last_received:
                raise IndependentOFIReject("Receiver time reversed")
            while i < len(times) and times[i] < recv:
                check(times[i])
                i += 1
            for event in msg["events"]:
                product = event["product_id"]
                if product not in books:
                    raise IndependentOFIReject("Unexpected market")
                prior = books[product].top()
                books[product].set_levels(event["type"], event["updates"])
                current = books[product].top()
                if event["type"] == "update":
                    e = independent_delta(prior, current)
                    windows[product].append((recv, e))
                    totals[product] += e
                last_l2[product] = recv
                last_seq[product] = n
            last_received = recv
    while i < len(times):
        check(times[i])
        i += 1
    if compares != len(expected):
        raise IndependentOFIReject("Not all expected rows checked")
    if (root / "manifest.json").read_bytes() != mb:
        raise IndependentOFIReject("Source manifest changed")
    return {
        "status": "INDEPENDENT_SOFTWARE_AGREEMENT_ON_SAME_COINBASE_SOURCE_NOT_OOS",
        "source_manifest_sha256": sha256(mb).hexdigest(),
        "source_message_count": src_seq + 1,
        "decision_rows_checked": compares,
        "independent_ofi_rows_verified": sum(
            r["status"] == "DIAGNOSTIC_CAUSAL_L1" for r in expected
        ),
        "exchange_clock_or_independent_venue_verified": False,
        "alpha_or_fills_verified": False,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", type=Path)
    ap.add_argument("--report", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    result = independent_verify(args.source, json.loads(args.report.read_text()))
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(result["status"], result["decision_rows_checked"])


if __name__ == "__main__":
    main()
