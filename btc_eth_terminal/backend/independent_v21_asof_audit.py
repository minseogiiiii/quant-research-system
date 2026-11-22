"""Second-software replay of receiver-time L2 quotes, without importing
v21_source_evidence's extractor. Tests parity of exact source-sequence, quote
price/size, quote age and accept/reject state for the frozen 5-minute grid.
Uses the SAME original archive, not independent exchange truth or fill proof.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from independent_soak_replay import replay as independent_source_replay

PRODUCTS = ("BTC-USD", "ETH-USD")
STEP = 300000


def ms(t):
    if not isinstance(t, str) or not t.endswith("Z"):
        raise ValueError("UTC source receipt required")
    return int(datetime.fromisoformat(t[:-1] + "+00:00").timestamp() * 1000)


def independent_quotes(root):
    root = Path(root)
    src = independent_source_replay(root)
    mb = (root / "manifest.json").read_bytes()
    manifest = json.loads(mb)
    start = ((ms(manifest["sampled_from_utc"]) + STEP - 1) // STEP) * STEP
    end = ms(manifest["finished_at_utc"])
    decisions = list(range(start, end + 1, STEP))
    b = {p: {"bid": {}, "offer": {}} for p in PRODUCTS}
    last = {p: None for p in PRODUCTS}
    seq = {p: None for p in PRODUCTS}
    ready = {p: False for p in PRODUCTS}
    out = []
    k = 0

    def report(d):
        for p in PRODUCTS:
            book = b[p]
            bid = max(book["bid"]) if book["bid"] else None
            ask = min(book["offer"]) if book["offer"] else None
            age = None if last[p] is None else d - last[p]
            if age is not None and age < 0:
                raise ValueError("Future L2 quote used")
            good = (
                ready[p]
                and bid is not None
                and ask is not None
                and bid < ask
                and age is not None
                and age <= 5000
            )
            r = {
                "instrument": p,
                "decision_receive_utc_ms": d,
                "last_l2_received_utc_ms": last[p],
                "source_message_sequence": seq[p],
                "quote_age_ms": age,
                "status": "RECEIVER_ASOF_L2_QUOTE"
                if good
                else "NO_ADMISSIBLE_FRESH_QUOTE",
            }
            if good:
                r.update(
                    best_bid=str(bid),
                    best_ask=str(ask),
                    spread=str(ask - bid),
                    displayed_best_bid_qty=str(book["bid"][bid]),
                    displayed_best_ask_qty=str(book["offer"][ask]),
                )
            out.append(r)

    for c in manifest["chunks"]:
        with (root / c["file"]).open("rb") as f:
            for line in f:
                row = json.loads(line)
                recv = ms(row["received_utc"])
                msg = json.loads(row["raw_text"])
                while k < len(decisions) and decisions[k] < recv:
                    report(decisions[k])
                    k += 1
                if msg.get("channel") != "l2_data":
                    continue
                for e in msg["events"]:
                    p = e["product_id"]
                    if e["type"] == "snapshot":
                        if ready[p]:
                            raise ValueError("Duplicate same-product snapshot")
                        ready[p] = True
                        b[p] = {"bid": {}, "offer": {}}
                    elif not ready[p]:
                        raise ValueError("Missing initial snapshot")
                    for change in e["updates"]:
                        side = change["side"]
                        px = Decimal(change["price_level"])
                        q = Decimal(change["new_quantity"])
                        if q == 0:
                            b[p][side].pop(px, None)
                        else:
                            b[p][side][px] = q
                    last[p] = recv
                    seq[p] = msg["sequence_num"]
    while k < len(decisions):
        report(decisions[k])
        k += 1
    if len(out) != 2 * len(decisions):
        raise ValueError("Wrong report size")
    return {
        "status": "INDEPENDENT_SOFTWARE_ASOF_PARITY_CHECK_ONLY",
        "source_sha256": sha256(mb).hexdigest(),
        "source_replay_messages": src["messages"],
        "source_books_final_sha256": {
            p: src["books_final"][p]["sha256"] for p in PRODUCTS
        },
        "rows": out,
        "independent_market_source_verified": False,
    }


def audit(root, canonical):
    primary = json.loads(Path(canonical).read_bytes())
    other = independent_quotes(root)
    if primary["source_manifest_sha256"] != other["source_sha256"]:
        raise ValueError("Source manifest identity mismatch")
    if (
        primary["source_message_count_independently_admitted"]
        != other["source_replay_messages"]
    ):
        raise ValueError("Original message-count mismatch")
    if primary["source_books_final_sha256"] != other["source_books_final_sha256"]:
        raise ValueError("Full-book final SHA-256 mismatch")
    if len(primary["rows"]) != len(other["rows"]):
        raise ValueError("As-of quote row count disagrees")
    required = (
        "instrument",
        "decision_receive_utc_ms",
        "last_l2_received_utc_ms",
        "source_message_sequence",
        "quote_age_ms",
        "status",
        "best_bid",
        "best_ask",
        "spread",
        "displayed_best_bid_qty",
        "displayed_best_ask_qty",
    )
    for i, (a, b) in enumerate(zip(primary["rows"], other["rows"], strict=False)):
        for field in required:
            if a.get(field) != b.get(field):
                raise ValueError(f"As-of quote discrepancy row {i}: {field}")
    return {
        "status": "TWO_SOFTWARE_ASOF_QUOTE_PARITY_PASSED",
        "rows_compared": len(other["rows"]),
        "source_messages": other["source_replay_messages"],
        "source_manifest_sha256": other["source_sha256"],
        "independent_market_source_verified": False,
        "clock_calibration_verified": False,
        "actual_original_feature_pair_audited": False,
        "trading_fills_or_alpha_verified": False,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("archive", type=Path)
    ap.add_argument("--canonical", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    r = audit(args.archive, args.canonical)
    args.output.write_text(json.dumps(r, indent=2, sort_keys=True) + "\n")
    print(r["status"], r["rows_compared"])
