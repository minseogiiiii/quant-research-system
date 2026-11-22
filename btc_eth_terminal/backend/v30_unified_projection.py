"""Read-only canonical *projection* of a previously audited Coinbase BTC/ETH source.

This is not a new collector, order-book engine, venue-OHLCV feed, or an OOS label.
It reuses the archived source validator and the existing v23 L1 feature engine.
All calendar bars below are RECEIVER-TIME AGGREGATES OF OBSERVED TRADE UPDATES,
not official exchange candles. Partial first/last buckets are intentionally omitted.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _q14_load_after_path_setup_0():
    from audit_soak import audit as existing_archive_audit
    from independent_soak_replay import replay as independent_archive_audit
    from v21_source_evidence import iso_ms

    return existing_archive_audit, independent_archive_audit, iso_ms


existing_archive_audit, independent_archive_audit, iso_ms = (
    _q14_load_after_path_setup_0()
)
del _q14_load_after_path_setup_0

PRODS = ("BTC-USD", "ETH-USD")
SCOPE = "COINBASE_PUBLIC_SPOT_BTC_ETH"
SCHEMA = "v30_unified_historical_projection_v1"
LIMIT_TRADES_PER_5M = 25
MAX_CHUNKS = 10_000
BASE_ROOT = HERE / "data" / "live_v16_one_hour" / "coinbase_btc_eth_soak_eE9SNH"
REFERENCE = HERE.parent / "V21_SOURCE_EVIDENCE_REPORT.json"


class ProjectionError(ValueError):
    pass


def _strict_json(raw: bytes):
    def pairs(items):
        obj = {}
        for k, v in items:
            if k in obj:
                raise ProjectionError("Duplicate JSON key")
            obj[k] = v
        return obj

    def constant(x):
        raise ProjectionError("Nonfinite JSON token")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def _price(value):
    if not isinstance(value, str):
        raise ProjectionError("Non-decimal trade data")
    try:
        v = Decimal(value)
    except InvalidOperation as exc:
        raise ProjectionError("Invalid price/size") from exc
    if not v.is_finite() or v <= 0:
        raise ProjectionError("Invalid nonpositive trade")
    return v


def _status(a):
    if (
        a.get("status") != "INTERNALLY_REPLAYED_NO_KNOWN_GAP"
        or a.get("connection_count") != 1
    ):
        raise ProjectionError("Source was not independently admitted")


def build(source: Path = BASE_ROOT, reference: Path = REFERENCE) -> dict:
    source = Path(source)
    if source.is_symlink():
        raise ProjectionError("Symlink source root")
    source = source.resolve(strict=True)
    mpath = source / "manifest.json"
    mbytes = mpath.read_bytes()
    digest = sha256(mbytes).hexdigest()
    manifest = _strict_json(mbytes)
    if (
        not isinstance(manifest.get("chunks"), list)
        or len(manifest["chunks"]) > MAX_CHUNKS
    ):
        raise ProjectionError("Unbounded source")
    audited = existing_archive_audit(source)
    independently = independent_archive_audit(source)
    _status(audited)
    if (
        independently.get("status") != "INDEPENDENT_SOURCE_REPLAY_ONLY"
        or independently.get("connections") != 1
    ):
        raise ProjectionError("Independent source replay not admissible")
    if audited["message_count"] != independently["messages"]:
        raise ProjectionError("Independent replay count disagreement")
    try:
        ref = _strict_json(Path(reference).read_bytes())
    except OSError as exc:
        raise ProjectionError("Missing baseline reference") from exc
    if ref.get("source_manifest_sha256") != digest:
        raise ProjectionError("Source lineage mismatch")
    # Reuse the frozen, previously independently crosschecked v23 diagnostic.
    # Its audit status is deliberately no stronger than receiver-only.
    causal = _strict_json(
        (HERE.parent / "V23_CAUSAL_MICROSTRUCTURE_DIAGNOSTIC.json").read_bytes()
    )
    if (
        causal.get("source_manifest_sha256") != digest
        or causal.get("eligible_for_oos_or_execution") is not False
        or len(causal.get("results", [])) != len(ref.get("rows", []))
    ):
        raise ProjectionError("Unsafe or missing frozen feature report")
    for row, rr in zip(causal["results"], ref["rows"]):
        if (
            row.get("instrument") != rr.get("instrument")
            or row.get("decision_receive_utc_ms") != rr.get("decision_receive_utc_ms")
            or row.get("source_message_sequence") != rr.get("source_message_sequence")
            or row.get("feature_max_received_utc_ms")
            != rr.get("last_l2_received_utc_ms")
        ):
            raise ProjectionError("Frozen feature and receiver-asof reference disagree")
    # Build a bounded trade tape and receiver-time aggregate. Ignore the
    # startup trade snapshot: it is historical context, not newly observed flow.
    first_ms = iso_ms(manifest["sampled_from_utc"])
    last_ms = iso_ms(manifest["finished_at_utc"])
    first_complete = ((first_ms + 299999) // 300000) * 300000
    last_complete = (last_ms // 300000) * 300000
    buckets = {p: {} for p in PRODS}
    trade_tail_by_bucket = {p: {} for p in PRODS}
    counts = {p: 0 for p in PRODS}
    prev_seq = None
    for chunk in manifest["chunks"]:
        f = source / chunk["file"]
        if f.is_symlink() or not f.is_file() or not f.resolve().is_relative_to(source):
            raise ProjectionError("Unsafe chunk path")
        raw = f.read_bytes()
        if sha256(raw).hexdigest() != chunk["archive_sha256"]:
            raise ProjectionError("Source chunk changed during projection")
        for line in raw.splitlines():
            envelope = _strict_json(line)
            if sha256(envelope["raw_text"].encode()).hexdigest() != envelope["sha256"]:
                raise ProjectionError("Message digest mismatch")
            msg = _strict_json(envelope["raw_text"].encode())
            seq = msg["sequence_num"]
            if prev_seq is not None and seq != prev_seq + 1:
                raise ProjectionError("Missing original message")
            prev_seq = seq
            if msg["channel"] != "market_trades":
                continue
            recv = iso_ms(envelope["received_utc"])
            for evt in msg["events"]:
                if evt.get("type") != "update":
                    continue
                for item in evt.get("trades", []):
                    p = item["product_id"]
                    if p not in PRODS:
                        raise ProjectionError("Unexpected instrument")
                    price = _price(item["price"])
                    size = _price(item["size"])
                    counts[p] += 1
                    trade = {
                        "received_at_ms": recv,
                        "message_seq": seq,
                        "trade_id": str(item["trade_id"]),
                        "exchange_time": item["time"],
                        "price": str(price),
                        "size": str(size),
                        "side_label_source": item.get("side"),
                        "product": p,
                    }
                    bucket = (recv // 300000) * 300000
                    tail = trade_tail_by_bucket[p].setdefault(bucket, [])
                    tail.append(trade)
                    if len(tail) > LIMIT_TRADES_PER_5M:
                        tail.pop(0)
                    if first_complete <= bucket < last_complete:
                        state = buckets[p].setdefault(
                            bucket,
                            {
                                "open": price,
                                "high": price,
                                "low": price,
                                "close": price,
                                "volume": Decimal(0),
                                "trades": 0,
                                "last_recv": recv,
                            },
                        )
                        state["high"] = max(state["high"], price)
                        state["low"] = min(state["low"], price)
                        state["close"] = price
                        state["volume"] += size
                        state["trades"] += 1
                        state["last_recv"] = recv
    # The original independent replay enforces unique trade IDs and checks
    # contradictory repeats. Do not aggregate duplicate update trade events.
    for product in PRODS:
        expected = independently["per_product"][product]["unique_update_trade"]
        if counts[product] != expected:
            raise ProjectionError(
                "Observed update trades disagree with independent dedup"
            )
    if (source / "manifest.json").read_bytes() != mbytes:
        raise ProjectionError("Manifest changed mid build")
    feature_by_product = {p: [] for p in PRODS}
    for row in causal["results"]:
        p = row["instrument"]
        feature_by_product[p].append(row)
    products = {}
    for p in PRODS:
        candles = []
        for timestamp, s in sorted(buckets[p].items()):
            candles.append(
                {
                    "time": timestamp,
                    "available_at": timestamp + 300000,
                    "open": str(s["open"]),
                    "high": str(s["high"]),
                    "low": str(s["low"]),
                    "close": str(s["close"]),
                    "volume": str(s["volume"]),
                    "observed_trade_count": s["trades"],
                    "last_trade_received_at": s["last_recv"],
                }
            )
        sampled = [
            t for bucket, tail in sorted(trade_tail_by_bucket[p].items()) for t in tail
        ]
        products[p] = {
            "candles": candles,
            "features": feature_by_product[p],
            "trades": sampled,
            "observed_updates_trades": counts[p],
        }
    result = {
        "schema": SCHEMA,
        "scope": SCOPE,
        "source_manifest_sha256": digest,
        "source_message_count": audited["message_count"],
        "source_first_received_at_ms": first_ms,
        "source_finished_at_ms": last_ms,
        "source_identity": "Coinbase Advanced public spot WebSocket BTC-USD / ETH-USD",
        "source_cohort": "v16_one_hour_single_connection_20261114",
        "source_admission": "INTERNALLY_REPLAYED_NO_KNOWN_GAP",
        "source_clock_independently_calibrated": False,
        "oos_approved": False,
        "execution_authorized": False,
        "evidence_status": "HISTORICAL_SOURCE_AUDITED_PIT_NOT_VERIFIED",
        "candles_description": "5m receiver-time observed market_trades update aggregation; not exchange OHLCV, not exhaustive volume",
        "candles_first_last_partial_excluded": True,
        "trade_side_semantics": "Coinbase feed side field unmodified, not interpreted as aggressor",
        "features_description": "Existing v23 5m receiver-asof causal L1 best imbalance, microprice and rolling OFI",
        "products": products,
    }
    return result


def package_projection(source=BASE_ROOT, reference=REFERENCE, dest=None):
    projection = build(Path(source), Path(reference))
    dest = Path(dest or HERE / "data" / "unified_v30_one_hour" / "projection.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(
            projection, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        + "\n"
    ).encode()
    tmp = dest.with_name("." + dest.name + ".tmp")
    tmp.write_bytes(encoded)
    tmp.replace(dest)
    manifest = {
        "schema": "v30_unified_projection_digest_v1",
        "projection_sha256": sha256(encoded).hexdigest(),
        "source_manifest_sha256": projection["source_manifest_sha256"],
        "source_message_count": projection["source_message_count"],
        "evidence_label": "RECEIVER_ASOF_HISTORICAL_REPLAY_NOT_OOS",
    }
    out = dest.with_name("projection.sha256.json")
    out.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=BASE_ROOT)
    parser.add_argument("--reference", type=Path, default=REFERENCE)
    parser.add_argument(
        "--output",
        type=Path,
        default=HERE / "data" / "unified_v30_one_hour" / "projection.json",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            package_projection(args.source, args.reference, args.output), indent=2
        )
    )
