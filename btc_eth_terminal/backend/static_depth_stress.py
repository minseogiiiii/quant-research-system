"""Read-only, point-in-observation static L2 depth stress diagnostics for BTC/ETH.

This module does NOT simulate a fill. Displayed L2 liquidity can cancel, move, be
queue-ahead, or be depleted by other orders; a frozen-book depth walk is neither
an order-routing simulation nor a market-impact estimator.

The quote packet is fixed by the existing v15 first-eligible-post-delay engine.
An independently retained, SHA-verified archive replay then inspects up to an
explicit number of same-side best price levels at *that exact packet*. No later
quote selection, execution API, fees inferred from exchange, ML or OOS training.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from taker_quote_envelope import (
    BPS,
    Assumptions,
    Book,
    evaluate,
    integer,
    number,
    unique_json,
)


class DepthStressError(ValueError):
    pass


@dataclass(frozen=True)
class DepthPolicy:
    max_levels: int
    max_static_sweep_bps: str

    def validated(self):
        integer(self.max_levels, "max_levels", 1, 50)
        return number(
            self.max_static_sweep_bps,
            "max_static_sweep_bps",
            allow_zero=True,
            upper=Decimal("5000"),
        )


def _static_ladder(
    book: Book, side: str, quantity: Decimal, fraction: Decimal, max_levels: int
):
    table = book.offer if side == "BUY" else book.bid
    ascending = side == "BUY"
    levels = sorted(table.items(), reverse=not ascending)[:max_levels]
    rem = quantity
    notional = Decimal(0)
    capacity = Decimal(0)
    used = []
    for price, visible in levels:
        available = visible * fraction
        capacity += available
        if rem <= 0:
            continue
        take = min(rem, available)
        if take <= 0:
            continue
        rem -= take
        notional += price * take
        used.append(
            {
                "price": str(price),
                "visible_quantity": str(visible),
                "haircut_quantity": str(available),
                "hypothetical_used_quantity": str(take),
            }
        )
    if rem > 0:
        return {
            "status": "REJECTED_STATIC_DEPTH_NOT_DEMONSTRATED_WITHIN_LEVEL_LIMIT",
            "available_haircut_quantity_within_levels": str(capacity),
            "quantity_uncovered": str(rem),
            "static_levels_examined": len(levels),
        }
    return {
        "status": "STATIC_L2_DEPTH_REFERENCE_NOT_FILL",
        "static_levels_examined": len(levels),
        "levels_referenced": used,
        "available_haircut_quantity_within_levels": str(capacity),
        "static_vwap": str(notional / quantity),
    }


def diagnose(
    source: Path, intents: list, assumptions: Assumptions, policy: DepthPolicy
) -> dict:
    """Evaluate specified intents without generating or choosing trading signals."""
    max_sweep_bps = policy.validated()
    fee, slippage, fraction = assumptions.validated()
    # Quote-admission performs a full independent original-source integrity check.
    baseline = evaluate(source, intents, assumptions)
    by_seq = {}
    for q in baseline["results"]:
        if q["status"] in (
            "INDICATIVE_TAKER_QUOTE_ONLY",
            "REJECTED_INSUFFICIENT_BEST_LEVEL_DEPTH",
        ):
            if "quote_sequence" not in q:
                raise DepthStressError("Missing first post-delay quote sequence")
            by_seq.setdefault(q["quote_sequence"], []).append(q)
    manifest_path = Path(source) / "manifest.json"
    if manifest_path.is_symlink():
        raise DepthStressError("Unsafe manifest symlink")
    manifest_data = manifest_path.read_bytes()
    if sha256(manifest_data).hexdigest() != baseline["source_manifest_sha256"]:
        raise DepthStressError("Source manifest changed after original quote admission")
    manifest = unique_json(manifest_data)
    books = {"BTC-USD": Book(), "ETH-USD": Book()}
    observations = {}
    count = 0
    last_mono = None
    root = Path(source).resolve()
    for chunk in manifest["chunks"]:
        p = Path(source) / chunk["file"]
        if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(root):
            raise DepthStressError("Unsafe or missing archived chunk")
        content = p.read_bytes()
        if (
            len(content) != chunk["archived_line_bytes"]
            or sha256(content).hexdigest() != chunk["archive_sha256"]
        ):
            raise DepthStressError("Changed raw source during depth replay")
        for line in content.splitlines(keepends=True):
            if not line.endswith(b"\n"):
                raise DepthStressError("Truncated archived source row")
            row = unique_json(line)
            raw = row["raw_text"]
            if (
                not isinstance(raw, str)
                or sha256(raw.encode()).hexdigest() != row["sha256"]
            ):
                raise DepthStressError("Source wrapper digest changed")
            msg = unique_json(raw)
            seq = msg["sequence_num"]
            if type(seq) is not int or seq != count:
                raise DepthStressError("Source sequence changed in depth replay")
            count += 1
            monotext = row["local_monotonic_ns"]
            if (
                not isinstance(monotext, str)
                or not monotext.isdecimal()
                or len(monotext) > 25
            ):
                raise DepthStressError("Invalid receipt clock")
            mono = int(monotext)
            if last_mono is not None and mono < last_mono:
                raise DepthStressError("Regressing receipt clock")
            last_mono = mono
            if msg["channel"] == "l2_data":
                for event in msg["events"]:
                    product = event["product_id"]
                    if product not in books:
                        raise DepthStressError("Unexpected product")
                    b = books[product]
                    if event["type"] == "snapshot":
                        b.replace()
                    for ch in event["updates"]:
                        b.change(
                            ch["side"],
                            number(ch["price_level"], "price"),
                            number(
                                ch["new_quantity"], "depth quantity", allow_zero=True
                            ),
                        )
            for q in by_seq.get(seq, []):
                b = books[q["product_id"]]
                bid, ask = b.best("bid"), b.best("offer")
                if bid is None or ask is None or bid[0] >= ask[0]:
                    raise DepthStressError("Invalid target book at quote packet")
                if q["status"] == "INDICATIVE_TAKER_QUOTE_ONLY" and (
                    str(bid[0]) != q["market_best_bid"]
                    or str(ask[0]) != q["market_best_ask"]
                    or str((ask if q["side"] == "BUY" else bid)[1])
                    != q["observed_best_level_qty"]
                ):
                    raise DepthStressError("Mismatch with existing first-quote model")
                if (
                    q["status"] == "REJECTED_INSUFFICIENT_BEST_LEVEL_DEPTH"
                    and str((ask if q["side"] == "BUY" else bid)[1])
                    != q["observed_best_level_qty"]
                ):
                    raise DepthStressError("Quote best-level quantity inconsistent")
                qty = number(
                    q["requested_quantity"],
                    "requested quantity",
                    upper=Decimal("1000000"),
                )
                r = _static_ladder(b, q["side"], qty, fraction, policy.max_levels)
                r.update(
                    {
                        "id": q["id"],
                        "product_id": q["product_id"],
                        "side": q["side"],
                        "quote_sequence": seq,
                        "original_quote_status": q["status"],
                        "hypothetical_quote_not_fill": True,
                    }
                )
                if r["status"] == "STATIC_L2_DEPTH_REFERENCE_NOT_FILL":
                    best_price = (ask if q["side"] == "BUY" else bid)[0]
                    static_vwap = Decimal(r["static_vwap"])
                    sweep = (
                        (
                            (static_vwap - best_price)
                            if q["side"] == "BUY"
                            else (best_price - static_vwap)
                        )
                        / best_price
                        * BPS
                    )
                    r["static_sweep_from_best_bps"] = str(sweep)
                    if sweep > max_sweep_bps:
                        r.update(status="REJECTED_STATIC_SWEEP_EXCEEDS_POLICY")
                        r.pop("static_vwap", None)
                        r.pop("levels_referenced", None)
                    else:
                        adjusted = (
                            static_vwap * (1 + slippage / BPS)
                            if q["side"] == "BUY"
                            else static_vwap * (1 - slippage / BPS)
                        )
                        hypothetical_notional = adjusted * qty
                        hypothetical_fee = hypothetical_notional * fee / BPS
                        r.update(
                            static_vwap_with_hypothetical_slippage=str(adjusted),
                            hypothetical_one_way_fee_usd=str(hypothetical_fee),
                            hypothetical_cashflow_usd=str(
                                -hypothetical_notional - hypothetical_fee
                                if q["side"] == "BUY"
                                else hypothetical_notional - hypothetical_fee
                            ),
                        )
                if q["id"] in observations:
                    raise DepthStressError("Duplicate quote observation ID")
                observations[q["id"]] = r
    if count != baseline["source_messages_audited"]:
        raise DepthStressError("Archive length changed in depth replay")
    if manifest_path.read_bytes() != manifest_data:
        raise DepthStressError("Manifest changed during depth replay")
    if len(observations) != sum(map(len, by_seq.values())):
        raise DepthStressError("Some first qualifying quotes were not present")
    outcome = []
    for original in baseline["results"]:
        if original["id"] in observations:
            outcome.append(observations[original["id"]])
        else:
            outcome.append(
                {
                    "id": original["id"],
                    "product_id": original["product_id"],
                    "side": original["side"],
                    "quote_sequence": None,
                    "status": original["status"],
                    "hypothetical_quote_not_fill": True,
                }
            )
    return {
        "status": "CONDITIONAL_STATIC_L2_DEPTH_DIAGNOSTIC_ONLY",
        "source_manifest_sha256": baseline["source_manifest_sha256"],
        "source_messages_audited": count,
        "max_levels_predeclared": policy.max_levels,
        "max_static_sweep_bps_predeclared": str(max_sweep_bps),
        "per_level_available_fraction_hypothetical": str(fraction),
        "fee_bps_hypothetical": str(fee),
        "slippage_bps_hypothetical": str(slippage),
        "delay_ms_local_receipt_assumption": assumptions.latency_ms,
        "original_quote_source": (
            'v15 first later product L2 p'
            'acket after assumed latency'
        ),
        "results": outcome,
        "actual_order_sent": False,
        "actual_fills_verified": False,
        "market_impact_verified": False,
        "queue_priority_verified": False,
        "snapshot_depth_cancellable_and_not_executable_guarantee": True,
        "no_strategy_optimization_or_pnl": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--intents", type=Path, required=True)
    parser.add_argument("--latency-ms", type=int, required=True)
    parser.add_argument("--max-wait-ms", type=int, required=True)
    parser.add_argument("--max-signal-quote-age-ms", type=int, required=True)
    parser.add_argument("--fee-bps", required=True)
    parser.add_argument("--slippage-bps", required=True)
    parser.add_argument("--capacity-fraction", required=True)
    parser.add_argument("--max-levels", type=int, required=True)
    parser.add_argument("--max-static-sweep-bps", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    assumptions = Assumptions(
        args.latency_ms,
        args.max_wait_ms,
        args.max_signal_quote_age_ms,
        args.fee_bps,
        args.slippage_bps,
        args.capacity_fraction,
    )
    policy = DepthPolicy(args.max_levels, args.max_static_sweep_bps)
    intents = unique_json(args.intents.read_bytes())
    report = diagnose(args.source, intents, assumptions, policy)
    formatted = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(formatted)
    print(formatted)


if __name__ == "__main__":
    main()
