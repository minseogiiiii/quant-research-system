"""Read-only as-observed BTC/ETH spot top-of-book spread diagnostics.

This module is not a fill simulator, strategy, trading signal, PnL model,
or venue-execution permission. It only reports event-time *local-receipt*
quote spreads, conditioned on validated archived source and quote freshness.
No maker queue, market impact, borrow, funding, taxes, or live fee schedule
is inferred. Fee basis points are supplied explicitly or omitted.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path

from audit_soak import audit

PRODUCTS = ("BTC-USD", "ETH-USD")


def utc(v):
    if not isinstance(v, str):
        raise ValueError("UTC observation timestamp required")
    d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if d.tzinfo is None or d.utcoffset() != timedelta(0):
        raise ValueError("Clock offset not explicit UTC")
    return d


def d(v, zero=False):
    if not isinstance(v, str):
        raise ValueError("Decimal source text required")
    try:
        x = Decimal(v)
    except InvalidOperation as exc:
        raise ValueError("Invalid decimal value") from exc
    if not x.is_finite() or x < 0 or (x == 0 and not zero):
        raise ValueError("Invalid price/quantity sign or finiteness")
    return x


def quantile(sorted_nums, fraction):
    """Nearest-rank descriptor; not confidence interval or independent N."""
    if not sorted_nums:
        return None
    pos = max(0, min(len(sorted_nums) - 1, math.ceil(len(sorted_nums) * fraction) - 1))
    return round(float(sorted_nums[pos]), 7)


def inspect(
    folder: Path, *, step_seconds=5, max_quote_age_seconds=3, taker_fee_bps=None
) -> dict:
    if type(step_seconds) is not int or not 1 <= step_seconds <= 300:
        raise ValueError("step_seconds must be an integer from 1 to 300")
    if type(max_quote_age_seconds) is not int or not 1 <= max_quote_age_seconds <= 60:
        raise ValueError("quote freshness must be 1..60 seconds")
    fee = None
    if taker_fee_bps is not None:
        # Require explicit nonnegative finite value and do not default to a tier.
        try:
            fee = Decimal(str(taker_fee_bps))
        except InvalidOperation as exc:
            raise ValueError("Invalid fee tier") from exc
        if not fee.is_finite() or not 0 <= fee <= 100:
            raise ValueError("Explicit hypothetical one-way fee must be 0..100 bps")
    root = Path(folder).resolve()
    admission = audit(root)
    if not admission["eligible_for_point_in_time_feature_research"]:
        raise ValueError(
            "Gapped/invalid market archive not eligible for spread analysis"
        )
    manifest_bytes = (root / "manifest.json").read_bytes()
    mf = json.loads(manifest_bytes)
    books = {p: {"bid": {}, "offer": {}} for p in PRODUCTS}
    last_book_change = {p: None for p in PRODUCTS}
    initial_snapshots = set()
    samples = {p: [] for p in PRODUCTS}
    next_at = None
    n_considered = 0
    n_stale = {p: 0 for p in PRODUCTS}
    n_invalid = {p: 0 for p in PRODUCTS}

    def take_sample(t):
        for product in PRODUCTS:
            bid, ask = books[product]["bid"], books[product]["offer"]
            last = last_book_change[product]
            if product not in initial_snapshots or not bid or not ask or last is None:
                n_invalid[product] += 1
                continue
            if t - last > timedelta(seconds=max_quote_age_seconds):
                n_stale[product] += 1
                continue
            high_bid = max(bid)
            low_ask = min(ask)
            if low_ask <= high_bid:
                raise ValueError("Crossed/locked book observed")
            mid = (high_bid + low_ask) / 2
            spread_bp = (low_ask - high_bid) / mid * 10_000
            samples[product].append(spread_bp)

    for chunk in mf["chunks"]:
        source_path = root / chunk["file"]
        if (
            source_path.is_symlink()
            or not source_path.is_file()
            or not source_path.resolve().is_relative_to(root)
        ):
            raise ValueError("Unsafe or replaced source chunk")
        chunk_digest = sha256()
        with source_path.open("rb") as stream:
            for line in stream:
                if not line.endswith(b"\n") or len(line) > 18_500_000:
                    raise ValueError("Truncated or oversized source row")
                chunk_digest.update(line)
                wrapper = json.loads(line)
                raw_text = wrapper["raw_text"]
                if (
                    not isinstance(raw_text, str)
                    or sha256(raw_text.encode("utf-8")).hexdigest() != wrapper["sha256"]
                ):
                    raise ValueError("Source payload changed after initial audit")
                t = utc(wrapper["received_utc"])
                if next_at is None:
                    next_at = t
                # Use the strictly PREVIOUS observed book before consuming a
                # new event at t. This forbids peeking into future callbacks.
                while next_at <= t:
                    take_sample(next_at)
                    n_considered += 1
                    next_at += timedelta(seconds=step_seconds)
                raw = json.loads(raw_text)
                if raw["channel"] == "l2_data":
                    for event in raw["events"]:
                        product = event["product_id"]
                        if event["type"] == "snapshot":
                            initial_snapshots.add(product)
                            books[product] = {"bid": {}, "offer": {}}
                        for change in event["updates"]:
                            side = change["side"]
                            price = d(change["price_level"])
                            qty = d(change["new_quantity"], zero=True)
                            if qty == 0:
                                books[product][side].pop(price, None)
                            else:
                                books[product][side][price] = qty
                        last_book_change[product] = t
        if chunk_digest.hexdigest() != chunk["archive_sha256"]:
            raise ValueError("Source chunk bytes changed after first admission audit")
    if n_considered == 0:
        raise ValueError("Observation duration too short for sample interval")
    outcome = {}
    for product in PRODUCTS:
        ordered = sorted(samples[product])
        outcome[product] = {
            "fresh_quote_samples": len(ordered),
            "stale_quote_samples_excluded": n_stale[product],
            "unready_or_empty_book_samples_excluded": n_invalid[product],
            "spread_bps_min": round(float(ordered[0]), 7) if ordered else None,
            "spread_bps_median": round(float(statistics.median(ordered)), 7)
            if ordered
            else None,
            "spread_bps_p90_nearest_rank": quantile(ordered, 0.9),
            "spread_bps_max": round(float(ordered[-1]), 7) if ordered else None,
            "one_way_half_spread_plus_explicit_fee_bps_median": round(
                float(statistics.median(ordered) / 2 + fee), 7
            )
            if ordered and fee is not None
            else None,
        }
    return {
        "status": "OBSERVED_QUOTES_ONLY_NO_FILL_PROOF",
        "symbol_scope": ["BTC-USD", "ETH-USD"],
        "source_manifest_sha256": sha256(manifest_bytes).hexdigest(),
        "source_messages": admission["message_count"],
        "source_chunks": admission["chunk_count"],
        "asof_clock": "RECEIVER_LOCAL_UTC_NOT_EXCHANGE_ATOMIC",
        "sampling": "LAST_OBSERVED_BEFORE_GRID_TICK",
        "grid_seconds": step_seconds,
        "max_book_staleness_seconds": max_quote_age_seconds,
        "grid_ticks_considered": n_considered,
        "user_supplied_hypothetical_one_way_taker_fee_bp": str(fee)
        if fee is not None
        else None,
        "products": outcome,
        "actual_order_fills_verified": False,
        "historical_fee_tier_verified": False,
        "queue_position_or_impact_verified": False,
        "economic_alpha_verified": False,
        "no_orders_sent": True,
    }


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("source", type=Path)
    cli.add_argument("--step-seconds", type=int, default=5)
    cli.add_argument("--max-quote-age-seconds", type=int, default=3)
    cli.add_argument(
        "--taker-fee-bps",
        type=str,
        default=None,
        help="Optional explicit hypothetical fee. Never fetched or assumed.",
    )
    cli.add_argument("--report", type=Path)
    args = cli.parse_args()
    out = inspect(
        args.source,
        step_seconds=args.step_seconds,
        max_quote_age_seconds=args.max_quote_age_seconds,
        taker_fee_bps=args.taker_fee_bps,
    )
    rendered = json.dumps(out, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.write_text(rendered)
    print(rendered)


if __name__ == "__main__":
    main()
