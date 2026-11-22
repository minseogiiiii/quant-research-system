"""Audited ten-level L2 and strictly post-hoc receiver-time replenishment study.

This is a historical diagnostic, not a live market, exchange-attested event clock,
alpha factor, fill simulator, or trading system. The existing independent source
checks and frozen v23 point-in-time L1 decisions remain authoritative.
"""

from __future__ import annotations

import argparse
import heapq
import json
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from statistics import median

from v23_causal_microstructure import STALENESS_MS, Book, FeatureReject, read_l2
from v30_unified_api import PROJECTION as BASE_PROJECTION
from v30_unified_api import load_projection
from v30_unified_projection import BASE_ROOT, HERE, PRODS, _strict_json

OUT = HERE / "data" / "v34_l2_depth"
SCHEMA = "v34_receiver_time_l2_depth_v1"
DEPTH = 10
SAMPLE_MS = 1_000
BASELINE_SAMPLES = 30
RECOVERY_SAMPLES = 30
SHOCK_FRACTION = Decimal("0.5")
RECOVERY_FRACTION = Decimal("0.8")
COOLDOWN_MS = 60_000
MAX_RAW_MESSAGES = 1_000_000


class DepthError(ValueError):
    pass


def top_levels(book: Book):
    if not book.initialized:
        return None
    bid_prices = heapq.nlargest(DEPTH, book.level["bid"])
    ask_prices = heapq.nsmallest(DEPTH, book.level["offer"])
    bid = [(p, book.level["bid"][p]) for p in bid_prices]
    ask = [(p, book.level["offer"][p]) for p in ask_prices]
    if not bid or not ask or bid[0][0] >= ask[0][0]:
        raise DepthError("No valid uncrossed full-depth source quote")
    if len(bid) < DEPTH or len(ask) < DEPTH:
        raise DepthError("Not enough historical L2 levels")

    def encode(levels):
        running = Decimal(0)
        out = []
        for price, qty in levels:
            if qty <= 0 or price <= 0:
                raise DepthError("Invalid depth level")
            running += qty
            out.append(
                {
                    "price": str(price),
                    "quantity": str(qty),
                    "cumulative_base": str(running),
                }
            )
        return out

    return {
        "bids": encode(bid),
        "asks": encode(ask),
        "bid_total_base_10": str(sum((q for _, q in bid), Decimal(0))),
        "ask_total_base_10": str(sum((q for _, q in ask), Decimal(0))),
    }


def shock_events(samples: list[dict], *, product: str):
    """Fixed descriptive threshold with PAST-only baseline, FUTURE outcome gated by availability.

    Samples are end-of-receiver-second L2 observations, not an exchange-event
    series. No unknown future recovery is exposed before the 30-second window.
    """
    result = []
    last = -(10**30)
    # observations require contiguous 1s epochs; detect any sample gaps.
    for idx in range(BASELINE_SAMPLES, len(samples) - RECOVERY_SAMPLES):
        curr = samples[idx]
        ts = curr["time_ms"]
        if ts - last < COOLDOWN_MS:
            continue
        past = samples[idx - BASELINE_SAMPLES : idx]
        future = samples[idx + 1 : idx + RECOVERY_SAMPLES + 1]
        if any(
            s["time_ms"] != ts - (BASELINE_SAMPLES - j) * SAMPLE_MS
            for j, s in enumerate(past)
        ) or any(
            s["time_ms"] != ts + (j + 1) * SAMPLE_MS for j, s in enumerate(future)
        ):
            continue
        if not (
            curr["valid"]
            and all(s["valid"] for s in past)
            and all(s["valid"] for s in future)
        ):
            continue
        base = Decimal(str(median([Decimal(s["depth_base"]) for s in past])))
        depth = Decimal(str(curr["depth_base"]))
        if base <= 0 or depth > base * SHOCK_FRACTION:
            continue
        threshold = base * RECOVERY_FRACTION
        elapsed = next(
            (
                s["time_ms"] - ts
                for s in future
                if Decimal(str(s["depth_base"])) >= threshold
            ),
            None,
        )
        result.append(
            {
                "product": product,
                "shock_receiver_ms": ts,
                "available_at_ms": ts + RECOVERY_SAMPLES * SAMPLE_MS,
                "baseline_30s_median_depth_base": str(base),
                "shock_depth_base": str(depth),
                "recovery_threshold_base": str(threshold),
                "recovery_within_30s": elapsed is not None,
                "first_recovery_delay_ms": elapsed,
                "censored_at_30s": elapsed is None,
            }
        )
        last = ts
    return result


def build(*, root: Path = BASE_ROOT, reference: Path = BASE_PROJECTION) -> dict:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise DepthError("Untrusted original source directory")
    initial_manifest = (root / "manifest.json").read_bytes()
    source_hash = sha256(initial_manifest).hexdigest()
    # Frozen original/independent audits were already recorded with the v33 source.
    # The exact original digest and message count remain pinned by v30, and the
    # separate L2 parser rehashes every original chunk during this projection.
    baseline, base_hash = load_projection()
    expected_msg_count = baseline["source_message_count"]
    if (
        sha256(Path(reference).read_bytes()).hexdigest() != base_hash
        or baseline["source_manifest_sha256"] != source_hash
        or expected_msg_count > MAX_RAW_MESSAGES
    ):
        raise DepthError("Historical projection/source mismatch")
    manifest = _strict_json(initial_manifest)
    by_product = {p: baseline["products"][p]["features"] for p in PRODS}
    decisions = sorted(
        {f["decision_receive_utc_ms"] for ps in by_product.values() for f in ps}
    )
    if not decisions or any(
        decisions[i] >= decisions[i + 1] for i in range(len(decisions) - 1)
    ):
        raise DepthError("Invalid frozen receiver-time diagnostic grid")
    books = {p: Book() for p in PRODS}
    last_recv = {p: None for p in PRODS}
    last_seq = {p: None for p in PRODS}
    rows = {p: [] for p in PRODS}
    samples = {p: [] for p in PRODS}
    next_decision = 0
    next_sample = None

    def emit_decision(t: int):
        for p in PRODS:
            age = None if last_recv[p] is None else t - last_recv[p]
            valid = age is not None and 0 <= age <= STALENESS_MS
            if valid:
                try:
                    depth = top_levels(books[p])
                except DepthError:
                    depth = None
            else:
                depth = None
            rows[p].append(
                {
                    "product": p,
                    "decision_receiver_ms": t,
                    "source_age_ms": age,
                    "source_sequence": last_seq[p],
                    "status": "HISTORICAL_L2_TEN_LEVEL"
                    if depth
                    else "NO_ADMISSIBLE_L2",
                    "depth": depth,
                }
            )

    def emit_second(t: int):
        for p in PRODS:
            age = None if last_recv[p] is None else t - last_recv[p]
            valid = age is not None and 0 <= age <= STALENESS_MS
            # Avoid full sort for every second unless the source is fresh.
            if valid:
                try:
                    depth = top_levels(books[p])
                except DepthError:
                    depth = None
            else:
                depth = None
            if depth:
                total = Decimal(depth["bid_total_base_10"]) + Decimal(
                    depth["ask_total_base_10"]
                )
                samples[p].append(
                    {"time_ms": t, "depth_base": str(total), "valid": True}
                )
            else:
                samples[p].append({"time_ms": t, "depth_base": "0", "valid": False})

    for recv, seq, product, kind, updates in read_l2(root, manifest["chunks"]):
        if product not in books:
            raise DepthError("Unexpected L2 product")
        if next_sample is None:
            next_sample = ((recv + SAMPLE_MS - 1) // SAMPLE_MS) * SAMPLE_MS
        while next_decision < len(decisions) and decisions[next_decision] < recv:
            emit_decision(decisions[next_decision])
            next_decision += 1
        while next_sample < recv:
            emit_second(next_sample)
            next_sample += SAMPLE_MS
        # A snapshot starts a product book; no previous snapshot is carried across disconnections.
        try:
            books[product].apply(kind, updates)
        except (FeatureReject, KeyError, TypeError, ValueError) as exc:
            raise DepthError("L2 state transition rejected") from exc
        last_recv[product] = recv
        last_seq[product] = seq
    while next_decision < len(decisions):
        emit_decision(decisions[next_decision])
        next_decision += 1
    for p in PRODS:
        expected = by_product[p]
        if len(rows[p]) != len(expected):
            raise DepthError("Missing L2 decision")
        for actual, prev in zip(rows[p], expected):
            if actual["decision_receiver_ms"] != prev["decision_receive_utc_ms"]:
                raise DepthError("Decision time mismatch")
            if (
                actual["source_sequence"] != prev["source_message_sequence"]
                or actual["source_age_ms"] != prev["source_age_ms"]
            ):
                raise DepthError(
                    "Source sequence or receiver clock does not match frozen v23"
                )
            if prev["status"] == "DIAGNOSTIC_CAUSAL_L1":
                depth = actual["depth"]
                if not depth:
                    raise DepthError(
                        "Previously admitted L1 missing from full L2 replay"
                    )
                for side, price_key, quantity_key in [
                    ("bids", "best_bid", "best_bid_qty"),
                    ("asks", "best_ask", "best_ask_qty"),
                ]:
                    if Decimal(depth[side][0]["price"]) != Decimal(
                        prev[price_key]
                    ) or Decimal(depth[side][0]["quantity"]) != Decimal(
                        prev[quantity_key]
                    ):
                        raise DepthError(
                            "L2 top level differs from independently frozen L1"
                        )
            elif actual["depth"] is not None:
                raise DepthError("L2 must not be displayed when L1 was not admissible")
    if (root / "manifest.json").read_bytes() != initial_manifest:
        raise DepthError("Source manifest changed while processing")
    result = {
        "schema": SCHEMA,
        "source_manifest_sha256": source_hash,
        "base_projection_sha256": base_hash,
        "source_messages": expected_msg_count,
        "source_clock_independently_calibrated": False,
        "receiver_time_only": True,
        "historical_only": True,
        "live_feed": False,
        "oos_approved": False,
        "execution_authorized": False,
        "trading_enabled": False,
        "ten_level_definition": "Top 10 displayed order-book levels reconstructed by receiver-ordered L2 snapshot/update; base-unit displayed quantity; not executable queue certainty",
        "recovery_definition": "Post-hoc receiver-second-sampled L2 depth diagnostic, 30s trailing median baseline, 50% drop shock, 80% recovery within 30s; hypothesis/parameters unoptimized and not independently OOS validated",
        "recovery_window_ms": RECOVERY_SAMPLES * SAMPLE_MS,
        "products": {
            p: {
                "depth_snapshots": rows[p],
                "recovery_events": shock_events(samples[p], product=p),
                "seconds_observed": len(samples[p]),
            }
            for p in PRODS
        },
    }
    return result


def write_projection(*, root=BASE_ROOT, dest=OUT / "projection.json"):
    result = build(root=Path(root))
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path_tmp = dest.with_name("." + dest.name + ".tmp")
    path_tmp.write_bytes(raw)
    path_tmp.replace(dest)
    pin = {
        "schema": "v34_l2_depth_pin_v1",
        "projection_sha256": sha256(raw).hexdigest(),
        "source_manifest_sha256": result["source_manifest_sha256"],
        "base_projection_sha256": result["base_projection_sha256"],
    }
    (dest.parent / "projection.sha256.json").write_text(
        json.dumps(pin, sort_keys=True, indent=2) + "\n"
    )
    return {
        "pin": pin,
        "depth_points": {
            p: len(result["products"][p]["depth_snapshots"]) for p in PRODS
        },
        "recovery_events": {
            p: len(result["products"][p]["recovery_events"]) for p in PRODS
        },
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, default=BASE_ROOT)
    p.add_argument("--out", type=Path, default=OUT / "projection.json")
    a = p.parse_args()
    print(json.dumps(write_projection(root=a.root, dest=a.out), indent=2))
