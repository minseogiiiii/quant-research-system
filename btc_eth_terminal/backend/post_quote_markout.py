"""Independent historical *future-label only* midquote markout audit.

This is deliberately NOT an execution, fill, maker-queue, or profitability model.
The P1 quote engine chooses the first eligible packet without future hindsight.
Only after that source sequence has been fixed do we observe future same-product
L2 messages at fixed horizons, using the receiver's LOCAL MONOTONIC clock.

Never merge these labels into as-of signal features or use the seven pinned
examples for training / hyperparameter tuning. Public L2 has no verified order
arrival, queue position, execution guarantee, market impact or actual fees.
"""

from __future__ import annotations

import argparse
import heapq
import json
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from taker_quote_envelope import (
    BPS,
    Assumptions,
    evaluate,
    integer,
    number,
    unique_json,
)


class MarkoutError(ValueError):
    pass


@dataclass(frozen=True)
class MarkoutPolicy:
    horizons_ms: tuple[int, ...]
    first_observation_tolerance_ms: int

    def validated(self) -> tuple[int, ...]:
        if (
            not isinstance(self.horizons_ms, tuple)
            or not 1 <= len(self.horizons_ms) <= 6
        ):
            raise MarkoutError("Require 1..6 frozen horizon values")
        if (
            type(self.first_observation_tolerance_ms) is not int
            or not 1 <= self.first_observation_tolerance_ms <= 10_000
        ):
            raise MarkoutError("Invalid horizon observation tolerance")
        previous = 0
        for val in self.horizons_ms:
            integer(val, "horizon_ms", 100, 60_000)
            if val <= previous:
                raise MarkoutError("Horizon values must be distinct and increasing")
            previous = val
        return self.horizons_ms


class IndexedBook:
    """Unique-price lazy heaps avoid pathological repeated L2 updates.

    A price has at most one resident heap entry, including across a delete
    followed by re-add before that stale entry is popped.
    """

    def __init__(self):
        self.bid = {}
        self.offer = {}
        self.bid_heap = []
        self.offer_heap = []
        self.bid_enqueued = set()
        self.offer_enqueued = set()

    def replace(self):
        self.__init__()

    def change(self, side, price, quantity):
        if side not in ("bid", "offer"):
            raise MarkoutError("Unexpected L2 side")
        table = self.bid if side == "bid" else self.offer
        heap = self.bid_heap if side == "bid" else self.offer_heap
        present = self.bid_enqueued if side == "bid" else self.offer_enqueued
        if not quantity:
            table.pop(price, None)
        else:
            table[price] = quantity
            if price not in present:
                heapq.heappush(heap, -price if side == "bid" else price)
                present.add(price)
        if len(heap) > 4 * len(table) + 1024:
            heap[:] = [(-p if side == "bid" else p) for p in table]
            heapq.heapify(heap)
            present.clear()
            present.update(table)

    def best(self, side):
        if side not in ("bid", "offer"):
            raise MarkoutError("Unexpected best side")
        table = self.bid if side == "bid" else self.offer
        heap = self.bid_heap if side == "bid" else self.offer_heap
        present = self.bid_enqueued if side == "bid" else self.offer_enqueued
        while heap:
            price = -heap[0] if side == "bid" else heap[0]
            if price in table:
                return price, table[price]
            heapq.heappop(heap)
            present.remove(price)
        return None


def _quote_mid(book: IndexedBook) -> Decimal:
    bid = book.best("bid")
    ask = book.best("offer")
    if bid is None or ask is None or bid[0] <= 0 or bid[0] >= ask[0]:
        raise MarkoutError(
            "Missing, crossed or locked bid-ask at selected L2 observation"
        )
    return (bid[0] + ask[0]) / Decimal(2)


def inspect(
    source: Path, intents: list, assumptions: Assumptions, policy: MarkoutPolicy
) -> dict:
    horizons = policy.validated()
    # This independently audits all original chunks and fixes the exact earliest
    # feasible post-delay quote, before we inspect any future outcome label.
    baseline = evaluate(source, intents, assumptions)
    source = Path(source)
    manifest_path = source / "manifest.json"
    if source.is_symlink() or manifest_path.is_symlink():
        raise MarkoutError("Symlink source or manifest refused")
    original_manifest = manifest_path.read_bytes()
    if sha256(original_manifest).hexdigest() != baseline["source_manifest_sha256"]:
        raise MarkoutError("Manifest changed after quote selection")
    manifest = unique_json(original_manifest)
    selected = {}
    for q in baseline["results"]:
        if q["status"] in (
            "INDICATIVE_TAKER_QUOTE_ONLY",
            "REJECTED_INSUFFICIENT_BEST_LEVEL_DEPTH",
        ):
            seq = q.get("quote_sequence")
            if type(seq) is not int:
                raise MarkoutError("Missing quote sequence on future-label candidate")
            selected.setdefault(seq, []).append(q)
    book = {"BTC-USD": IndexedBook(), "ETH-USD": IndexedBook()}
    active = []
    results = {}
    root = source.resolve()
    seq_count = 0
    last_ns = None
    for chunk in manifest["chunks"]:
        chunk_file = source / chunk["file"]
        if (
            chunk_file.is_symlink()
            or not chunk_file.is_file()
            or not chunk_file.resolve().is_relative_to(root)
        ):
            raise MarkoutError("Missing/unsafe archived source chunk")
        payload = chunk_file.read_bytes()
        if (
            len(payload) != chunk["archived_line_bytes"]
            or sha256(payload).hexdigest() != chunk["archive_sha256"]
        ):
            raise MarkoutError("Source chunk mutated after first quote selection")
        for line in payload.splitlines(keepends=True):
            if not line.endswith(b"\n"):
                raise MarkoutError("Truncated source JSONL row")
            row = unique_json(line)
            raw = row["raw_text"]
            if (
                not isinstance(raw, str)
                or sha256(raw.encode("utf-8")).hexdigest() != row["sha256"]
            ):
                raise MarkoutError("Raw WebSocket message SHA changed")
            msg = unique_json(raw)
            seq = msg["sequence_num"]
            if type(seq) is not int or seq != seq_count:
                raise MarkoutError("Source sequence mismatch in outcome replay")
            seq_count += 1
            ns_text = row["local_monotonic_ns"]
            if (
                not isinstance(ns_text, str)
                or not ns_text.isdecimal()
                or len(ns_text) > 25
            ):
                raise MarkoutError("Invalid local monotonic receipt timestamp")
            ns = int(ns_text)
            if last_ns is not None and ns < last_ns:
                raise MarkoutError("Regressing source receipt clock")
            last_ns = ns
            affected = set()
            if msg["channel"] == "l2_data":
                for event in msg["events"]:
                    product = event["product_id"]
                    if product not in book:
                        raise MarkoutError("Unexpected L2 product")
                    b = book[product]
                    if event["type"] == "snapshot":
                        b.replace()
                    for change in event["updates"]:
                        b.change(
                            change["side"],
                            number(change["price_level"], "level price"),
                            number(
                                change["new_quantity"],
                                "level quantity",
                                allow_zero=True,
                            ),
                        )
                    affected.add(product)
            # Historic midprice labels are only attached to the FIRST subsequent
            # same-product L2 packet after each predeclared horizon. No last-known
            # quote substitution if a fresh quote is unavailable in tolerance.
            for state in active:
                if (
                    not state["pending"]
                    or seq <= state["quote_sequence"]
                    or state["product_id"] not in affected
                ):
                    continue
                elapsed_ns = ns - state["quote_ns"]
                for horizon in horizons:
                    if (
                        state["pending"][horizon] is not None
                        or elapsed_ns < horizon * 1_000_000
                    ):
                        continue
                    if (
                        elapsed_ns
                        > (horizon + policy.first_observation_tolerance_ms) * 1_000_000
                    ):
                        state["pending"][horizon] = {
                            "status": "LABEL_NOT_AVAILABLE_WITHIN_TOLERANCE",
                            "horizon_ms": horizon,
                            "first_later_quote_sequence": seq,
                            "observed_delay_ms": str(
                                Decimal(elapsed_ns) / Decimal(1_000_000)
                            ),
                        }
                    else:
                        new_mid = _quote_mid(book[state["product_id"]])
                        signed = Decimal(1) if state["side"] == "BUY" else Decimal(-1)
                        bps = (
                            signed
                            * (new_mid - state["quote_mid"])
                            / state["quote_mid"]
                            * BPS
                        )
                        state["pending"][horizon] = {
                            "status": "HISTORICAL_FUTURE_MIDPRICE_LABEL_ONLY",
                            "horizon_ms": horizon,
                            "label_observed_at_sequence": seq,
                            "label_available_at_received_utc": row["received_utc"],
                            "observed_delay_ms": str(
                                Decimal(elapsed_ns) / Decimal(1_000_000)
                            ),
                            "future_midprice": str(new_mid),
                            "side_signed_midprice_move_bps": str(bps),
                            "label_not_available_to_signal": True,
                        }
            # Only start an anchor when its predetermined quote sequence is
            # reached. Never back-date a label to the signal's observation.
            for original in selected.get(seq, []):
                product = original["product_id"]
                if product not in affected:
                    raise MarkoutError(
                        "Pinned quote packet does not update expected product"
                    )
                mid = _quote_mid(book[product])
                if "market_best_bid" in original and (
                    str(book[product].best("bid")[0]) != original["market_best_bid"]
                    or str(book[product].best("offer")[0])
                    != original["market_best_ask"]
                ):
                    raise MarkoutError("Pinned baseline quote has changed")
                state = {
                    "id": original["id"],
                    "product_id": product,
                    "side": original["side"],
                    "quote_sequence": seq,
                    "quote_ns": ns,
                    "quote_mid": mid,
                    "quote_status": original["status"],
                    "pending": {k: None for k in horizons},
                }
                active.append(state)
                if original["id"] in results:
                    raise MarkoutError("Duplicate label observation ID")
                results[original["id"]] = state
    if seq_count != baseline["source_messages_audited"]:
        raise MarkoutError("Unexpected source count across independent passes")
    if manifest_path.read_bytes() != original_manifest:
        raise MarkoutError("Source manifest mutated while computing future labels")
    out = []
    for q in baseline["results"]:
        if q["id"] not in results:
            out.append(
                {
                    "id": q["id"],
                    "product_id": q["product_id"],
                    "original_quote_status": q["status"],
                    "status": "NO_FUTURE_LABEL_WITHOUT_VALID_QUOTE",
                    "labels": [],
                }
            )
            continue
        state = results[q["id"]]
        labels = []
        for horizon in horizons:
            entry = state["pending"][horizon]
            if entry is None:
                entry = {
                    "status": "LABEL_NOT_AVAILABLE_BEFORE_SOURCE_END",
                    "horizon_ms": horizon,
                }
            labels.append(entry)
        out.append(
            {
                "id": q["id"],
                "product_id": q["product_id"],
                "side": q["side"],
                "signal_sequence": q["signal_sequence"],
                "original_quote_status": state["quote_status"],
                "quote_sequence": state["quote_sequence"],
                "quote_local_monotonic_ns": str(state["quote_ns"]),
                "quote_midprice": str(state["quote_mid"]),
                "status": "FUTURE_LABELS_SEPARATE_FROM_AS_OF_SIGNAL",
                "labels": labels,
            }
        )
    return {
        "status": "READ_ONLY_FUTURE_MIDPRICE_MARKOUT_LABELS_NO_FILLS",
        "source_manifest_sha256": baseline["source_manifest_sha256"],
        "source_messages_audited": seq_count,
        "horizons_ms_predeclared": list(horizons),
        (
            'first_observation_tolerance_'
            'ms_predeclared'
        ): policy.first_observation_tolerance_ms,
        "receipt_clock": "LOCAL_MONOTONIC_RECEIVE_ONLY_NOT_EXCHANGE_SYNC",
        "quote_selection": "FIRST_PRODUCT_L2_AFTER_USER_ASSUMED_LATENCY_NO_LOOKAHEAD",
        "future_labels_are_never_input_features": True,
        "research_intents_are_not_strategy_signals": True,
        "future_observation_changes_do_not_change_selected_quote": True,
        "results": out,
        "maker_queue_rank_known": False,
        "actual_partial_fills_measured": False,
        "actual_order_fills_verified": False,
        "broker_latency_calibrated": False,
        "venue_fee_tier_verified": False,
        "market_impact_estimated": False,
        "strategy_alpha_or_profitability_verified": False,
        "orders_enabled": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--intents", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    intents = unique_json(args.intents.read_bytes())
    rules = unique_json(args.policy.read_bytes())
    if set(rules) != {
        "horizons_ms",
        "first_observation_tolerance_ms",
        "latency_ms",
        "max_wait_ms",
        "max_signal_quote_age_ms",
        "one_way_taker_fee_bps",
        "extra_slippage_bps",
        "top_level_capacity_fraction",
    }:
        raise MarkoutError("Unknown markout policy field")
    assumptions = Assumptions(
        rules["latency_ms"],
        rules["max_wait_ms"],
        rules["max_signal_quote_age_ms"],
        rules["one_way_taker_fee_bps"],
        rules["extra_slippage_bps"],
        rules["top_level_capacity_fraction"],
    )
    horizons = rules["horizons_ms"]
    if not isinstance(horizons, list):
        raise MarkoutError("horizons_ms must be an explicit JSON list")
    result = inspect(
        args.source,
        intents,
        assumptions,
        MarkoutPolicy(tuple(horizons), rules["first_observation_tolerance_ms"]),
    )
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
