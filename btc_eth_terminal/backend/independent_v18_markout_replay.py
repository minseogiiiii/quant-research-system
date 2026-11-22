"""Independent second historical markout audit, not exchange-side truth.

Uses direct Decimal order-book dictionaries and independent earliest-horizon
checks. Does not call post_quote_markout.inspect, evaluate, Book or IndexBook.
The source is the same Coinbase archive, so this is SOFTWARE interpretation
parity, not a second independently sourced market record.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path

from audit_v16_one_hour import SOURCE_MANIFEST_SHA
from taker_quote_envelope import unique_json

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "backend/data/live_v16_one_hour/coinbase_btc_eth_soak_eE9SNH"
REPORT = ROOT / "P1_POST_QUOTE_MARKOUT_v18.json"
POLICY = ROOT / "examples/v18_predeclared_markout_policy.json"
REPORT_SHA = "83c148e992b3b369e897b0f98cfa46dd7ee1c960f749aa42ff388be907d5b6c0"


class IndependentMarkoutError(ValueError):
    pass


def _num(text, allow_zero=False):
    if not isinstance(text, str) or not 1 <= len(text) <= 32:
        raise IndependentMarkoutError("Nontext/oversize raw number")
    try:
        d = Decimal(text)
    except InvalidOperation as exc:
        raise IndependentMarkoutError("Malformed source number") from exc
    if not d.is_finite() or d < 0 or (d == 0 and not allow_zero):
        raise IndependentMarkoutError("Negative or nonfinite raw source quantity/price")
    return d


def verify() -> dict:
    if sha256(REPORT.read_bytes()).hexdigest() != REPORT_SHA:
        raise IndependentMarkoutError("Report bytes changed")
    frozen = unique_json(REPORT.read_bytes())
    rules = unique_json(POLICY.read_bytes())
    mf = SOURCE / "manifest.json"
    if mf.is_symlink():
        raise IndependentMarkoutError("Unsafe source manifest")
    mf_raw = mf.read_bytes()
    if sha256(mf_raw).hexdigest() != SOURCE_MANIFEST_SHA:
        raise IndependentMarkoutError("Source manifest mismatch")
    manifest = unique_json(mf_raw)
    if (
        manifest["messages"] != 152069
        or len(manifest["chunks"]) != 41
        or manifest["ready_for_research"] is not True
        or len(manifest["connections"]) != 1
        or manifest["faults"] != []
    ):
        raise IndependentMarkoutError("Unapproved or incomplete original source")
    anchors = {}
    for row in frozen["results"]:
        seq = row.get("quote_sequence")
        if (
            type(seq) is not int
            or row.get("status") != "FUTURE_LABELS_SEPARATE_FROM_AS_OF_SIGNAL"
        ):
            raise IndependentMarkoutError("Expected fixed quote missing")
        anchors.setdefault(seq, []).append(row)
    bids = {"BTC-USD": {}, "ETH-USD": {}}
    asks = {"BTC-USD": {}, "ETH-USD": {}}
    active = []
    label_count = 0
    count = 0
    last_mono = None
    source_root = SOURCE.resolve()
    for c in manifest["chunks"]:
        p = SOURCE / c["file"]
        if (
            p.is_symlink()
            or not p.is_file()
            or not p.resolve().is_relative_to(source_root)
        ):
            raise IndependentMarkoutError("Unsafe/missing raw chunk")
        raw = p.read_bytes()
        if (
            len(raw) != c["archived_line_bytes"]
            or sha256(raw).hexdigest() != c["archive_sha256"]
        ):
            raise IndependentMarkoutError("Original source chunk digest differs")
        for line in raw.splitlines(keepends=True):
            if not line.endswith(b"\n"):
                raise IndependentMarkoutError("Truncated source line")
            wrapper = unique_json(line)
            payload = wrapper["raw_text"]
            if (
                not isinstance(payload, str)
                or sha256(payload.encode("utf-8")).hexdigest() != wrapper["sha256"]
            ):
                raise IndependentMarkoutError("Wrapped source message altered")
            msg = unique_json(payload)
            seq = msg["sequence_num"]
            if type(seq) is not int or seq != count:
                raise IndependentMarkoutError("Missing/repeated sequence")
            count += 1
            ts = wrapper["local_monotonic_ns"]
            if not isinstance(ts, str) or not ts.isdecimal() or len(ts) > 25:
                raise IndependentMarkoutError("Invalid local clock text")
            mono = int(ts)
            if last_mono is not None and mono < last_mono:
                raise IndependentMarkoutError("Clock went backwards")
            last_mono = mono
            affected = set()
            if msg["channel"] == "l2_data":
                for event in msg["events"]:
                    product = event["product_id"]
                    if product not in bids:
                        raise IndependentMarkoutError("Unexpected instrument")
                    if event["type"] == "snapshot":
                        bids[product].clear()
                        asks[product].clear()
                    for level in event["updates"]:
                        side = level["side"]
                        if side not in ("bid", "offer"):
                            raise IndependentMarkoutError("Unexpected L2 side")
                        data = bids[product] if side == "bid" else asks[product]
                        px = _num(level["price_level"])
                        qty = _num(level["new_quantity"], allow_zero=True)
                        if qty == 0:
                            data.pop(px, None)
                        else:
                            data[px] = qty
                    affected.add(product)

            def mid(product):
                if not bids[product] or not asks[product]:
                    raise IndependentMarkoutError("Missing L2 side")
                top_bid = max(bids[product])
                top_ask = min(asks[product])
                if top_bid >= top_ask:
                    raise IndependentMarkoutError("Invalid crossed/locked quote")
                return (top_bid + top_ask) / Decimal(2)

            # All future packets are inspected only after an anchor is fixed.
            for state in active:
                if seq <= state["quote_seq"] or state["product"] not in affected:
                    continue
                elapsed = mono - state["mono"]
                for label in state["labels"]:
                    if label["_audited"] or elapsed < label["horizon_ms"] * 1_000_000:
                        continue
                    if (
                        elapsed
                        > (
                            label["horizon_ms"]
                            + rules["first_observation_tolerance_ms"]
                        )
                        * 1_000_000
                    ):
                        raise IndependentMarkoutError(
                            "Pinned future quote exceeds tolerance"
                        )
                    future = mid(state["product"])
                    move = (
                        (Decimal(1) if state["side"] == "BUY" else Decimal(-1))
                        * (future - state["mid"])
                        / state["mid"]
                        * Decimal(10_000)
                    )
                    if (
                        seq != label["label_observed_at_sequence"]
                        or str(future) != label["future_midprice"]
                        or str(move) != label["side_signed_midprice_move_bps"]
                        or wrapper["received_utc"]
                        != label["label_available_at_received_utc"]
                    ):
                        raise IndependentMarkoutError(
                            "Independent source-derived horizon disagrees"
                        )
                    label["_audited"] = True
                    label_count += 1
            for row in anchors.get(seq, []):
                if row["product_id"] not in affected:
                    raise IndependentMarkoutError(
                        "Quoted packet was not a product update"
                    )
                quote = mid(row["product_id"])
                if str(quote) != row["quote_midprice"]:
                    raise IndependentMarkoutError("Initial pinned midprice differs")
                active.append(
                    {
                        "quote_seq": seq,
                        "mono": mono,
                        "mid": quote,
                        "product": row["product_id"],
                        "side": row["side"],
                        "labels": [dict(x, _audited=False) for x in row["labels"]],
                    }
                )
    if count != 152069 or label_count != 21:
        raise IndependentMarkoutError("Incomplete independent future-horizon coverage")
    if mf.read_bytes() != mf_raw:
        raise IndependentMarkoutError(
            "Source manifest changed during independent replay"
        )
    return {
        "verified": True,
        "status": "INDEPENDENT_SOFTWARE_REPLAY_SAME_COINBASE_RAW_SOURCE_NOT_SECOND_VENUE",
        "messages": count,
        "frozen_quote_count": len(active),
        "historical_future_label_count": label_count,
        "source_manifest_sha256": SOURCE_MANIFEST_SHA,
        "actual_fills_verified": False,
        "independent_market_source_verified": False,
        "alpha_verified": False,
        "orders_enabled": False,
    }


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2, sort_keys=True))
