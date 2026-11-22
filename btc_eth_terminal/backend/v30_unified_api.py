"""Strict read-only API contract for the immutable v30 historical projection.

Projection was generated from an already audited real public Coinbase spot
archive; NOT venue-attested, live, OOS, or trading eligible.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from v30_unified_projection import (
    BASE_ROOT,
    HERE,
    PRODS,
    SCHEMA,
    _strict_json,
)

ROOT = HERE / "data" / "unified_v30_one_hour"
PROJECTION = ROOT / "projection.json"
PIN = ROOT / "projection.sha256.json"
MAX_PROJECTION_BYTES = 2_000_000
MAX_LIMIT = 400
KINDS = {"candles", "features", "trades"}


class UnifiedAPIError(ValueError):
    pass


def _checked_file(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise UnifiedAPIError("Missing, oversized or symlinked evidence")
    return path.read_bytes()


def load_projection() -> tuple[dict, str]:
    (
        'Rehash projection + pinned s'
        'ource manifest per request; '
        'not a raw-archive re-audit.'
    )
    content = _checked_file(PROJECTION, MAX_PROJECTION_BYTES)
    pinned = _strict_json(_checked_file(PIN, 8_000))
    digest = sha256(content).hexdigest()
    if digest != pinned.get("projection_sha256"):
        raise UnifiedAPIError("Projection changed since frozen build")
    projection = _strict_json(content)
    if (
        projection.get("schema") != SCHEMA
        or projection.get("source_manifest_sha256")
        != pinned.get("source_manifest_sha256")
        or projection.get("source_message_count") != pinned.get("source_message_count")
        or projection.get("oos_approved") is not False
        or projection.get("execution_authorized") is not False
        or set(projection.get("products", {})) != set(PRODS)
    ):
        raise UnifiedAPIError("Projection identity, scope or safety fields invalid")
    raw_manifest = _checked_file(BASE_ROOT / "manifest.json", 5_000_000)
    if sha256(raw_manifest).hexdigest() != projection["source_manifest_sha256"]:
        raise UnifiedAPIError("Underlying market source manifest changed")
    return projection, digest


def _number(value, name, *, optional=False):
    if value is None and optional:
        return None
    if type(value) is not int or value < 0 or value > 8_000_000_000_000:
        raise UnifiedAPIError("Invalid " + name)
    return value


def _prod(product):
    if product not in PRODS:
        raise UnifiedAPIError("Only Coinbase BTC-USD / ETH-USD spot permitted")
    return product


def catalog():
    data, digest = load_projection()
    return {
        "schema": SCHEMA,
        "source_sha256": data["source_manifest_sha256"],
        "projection_sha256": digest,
        "source_message_count": data["source_message_count"],
        "source_start_ms": data["source_first_received_at_ms"],
        "source_end_ms": data["source_finished_at_ms"],
        "source_cohort": data["source_cohort"],
        "source_identity": data["source_identity"],
        "evidence_status": data["evidence_status"],
        "receiver_time_only": True,
        "live_feed": False,
        "point_in_time_historical_available_at_certified": False,
        "original_feature_label_pit_verified": False,
        "oos_approved": False,
        "trading_enabled": False,
        "products": [
            {
                "product": p,
                "candles": len(data["products"][p]["candles"]),
                "diagnostics": len(data["products"][p]["features"]),
                "recent_trades": len(data["products"][p]["trades"]),
                "observed_trade_updates": data["products"][p][
                    "observed_updates_trades"
                ],
            }
            for p in PRODS
        ],
        "candles_definition": data["candles_description"],
        "features_definition": data["features_description"],
    }


def series(
    product: str,
    kind: str,
    from_ms: int,
    to_ms: int,
    limit: int = 100,
    asof_ms: int | None = None,
    offset: int = 0,
):
    _prod(product)
    if kind not in KINDS:
        raise UnifiedAPIError("Unsupported series")
    lo = _number(from_ms, "from")
    hi = _number(to_ms, "to")
    asof = _number(asof_ms, "asof", optional=True)
    if lo >= hi:
        raise UnifiedAPIError("Invalid interval")
    if type(limit) is not int or limit < 1 or limit > MAX_LIMIT:
        raise UnifiedAPIError("Unbounded query limit")
    if type(offset) is not int or offset < 0 or offset > 50_000:
        raise UnifiedAPIError("Unbounded query offset")
    data, digest = load_projection()
    full = data["products"][product][kind]
    time_key = {
        "candles": "time",
        "features": "decision_receive_utc_ms",
        "trades": "received_at_ms",
    }[kind]
    filtered = []
    for entry in full:
        t = entry[time_key]
        if not lo <= t < hi:
            continue
        # A receiver-time candle is only *displayable* once its full interval ends.
        available = entry["available_at"] if kind == "candles" else t
        if asof is not None and available > asof:
            continue
        filtered.append(entry)
    sliced = filtered[offset : offset + limit]
    next_offset = (offset + limit) if offset + limit < len(filtered) else None
    return {
        "product": product,
        "kind": kind,
        "rows": sliced,
        "next_offset": next_offset,
        "source_sha256": data["source_manifest_sha256"],
        "projection_sha256": digest,
        "asof_receiver_ms": asof,
        "evidence_status": data["evidence_status"],
        "live_feed": False,
        "oos_approved": False,
        "trading_enabled": False,
    }


def book(product: str, asof_ms: int):
    _prod(product)
    asof = _number(asof_ms, "asof")
    data, digest = load_projection()
    matching = [
        x
        for x in data["products"][product]["features"]
        if x["decision_receive_utc_ms"] <= asof
    ]
    latest = matching[-1] if matching else None
    if latest and (
        latest["status"] != "DIAGNOSTIC_CAUSAL_L1"
        or asof - latest["decision_receive_utc_ms"] > 5000
    ):
        latest = None
    return {
        "product": product,
        "asof_receiver_ms": asof,
        "book": latest,
        "l1_only": True,
        "source_sha256": data["source_manifest_sha256"],
        "projection_sha256": digest,
        "no_book_if_invalid": True,
        "evidence_status": "REPLAY_VERIFIED_RECEIVER_ONLY_NOT_OOS"
        if latest
        else "NO_ADMISSIBLE_QUOTE",
        "live_feed": False,
        "trading_enabled": False,
    }
