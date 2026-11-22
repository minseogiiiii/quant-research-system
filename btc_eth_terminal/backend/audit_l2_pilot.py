"""Independent audit of a short public Coinbase L2/trades text-message recording.

No order execution, no alpha, no claim of independent venue reconciliation.
A source-receipt capture is not proof of full packet continuity or fill quality.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


class L2AuditError(ValueError):
    pass


def _unique_pairs(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise L2AuditError("Duplicate key in recorded WebSocket JSON")
        result[k] = v
    return result


def _bad_json_constant(t):
    raise L2AuditError("Non-JSON numeric value")


def _json(s):
    try:
        return json.loads(
            s, object_pairs_hook=_unique_pairs, parse_constant=_bad_json_constant
        )
    except (ValueError, UnicodeError, TypeError) as exc:
        raise L2AuditError("Invalid or ambiguous JSON payload") from exc


def _utc(s):
    if not isinstance(s, str):
        raise L2AuditError("Timestamp not a string")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, OverflowError) as exc:
        raise L2AuditError("Invalid UTC timestamp") from exc
    if dt.utcoffset() is None or dt.utcoffset().total_seconds() != 0:
        raise L2AuditError("Non-UTC timestamp")
    return dt


def _decimal(s, *, zero_ok=False):
    if not isinstance(s, str) or not s.strip():
        raise L2AuditError("Price/quantity must be a decimal string")
    try:
        val = Decimal(s)
    except InvalidOperation as exc:
        raise L2AuditError("Nondecimal price/quantity") from exc
    if not val.is_finite() or val < 0 or (val == 0 and not zero_ok):
        raise L2AuditError("Nonfinite or invalid price/quantity")
    return val


def audit_session(folder: Path, *, allow_exact_trade_replays: bool = False) -> dict:
    folder = folder.resolve()
    status_path = folder / "capture_status.json"
    raw_path = folder / "raw_websocket_payloads.jsonl"
    if not all(
        p.is_file() and not p.is_symlink() and p.resolve().parent == folder
        for p in (status_path, raw_path)
    ):
        raise L2AuditError("Missing or nonlocal recording")
    if raw_path.stat().st_size > 40_000_000:
        raise L2AuditError("Too large for bounded short-session audit")
    status = _json(status_path.read_bytes())
    if status.get("status") != "PUBLIC_L2_TEXT_CAPTURED_NOT_RECONSTRUCTED":
        raise L2AuditError("Capture did not report complete snapshot reception")
    expected = ("BTC-USD", "ETH-USD")
    if (
        status.get("products") != list(expected)
        or status.get("endpoint") != "wss://advanced-trade-ws.coinbase.com"
        or status.get("orders_enabled") is not False
    ):
        raise L2AuditError("Source or permissions mismatch")
    counts = Counter()
    snapshot_counts = Counter()
    update_counts = Counter()
    trade_records = Counter()
    trade_by_type = Counter()
    observed_trade_ids = {}
    duplicate_identical_trade_updates = Counter()
    snapshot_levels = Counter()
    channels = Counter()
    side_updates = Counter()
    invalid_books = 0
    books = {p: {"bid": {}, "offer": {}} for p in expected}
    sequence = []
    digest = hashlib.sha256()
    last_ns = -1
    first_recv = None
    last_recv = None
    payload_size = 0
    rows = 0
    with raw_path.open("rb") as file:
        for n, line in enumerate(file, 1):
            if len(line) > 14_000_000 or not line.endswith(b"\n"):
                raise L2AuditError("Truncated or oversized JSONL record")
            entry = _json(line)
            text = entry.get("raw_text")
            if not isinstance(text, str):
                raise L2AuditError("Missing original WS UTF8 text")
            message_bytes = text.encode("utf-8")
            if hashlib.sha256(message_bytes).hexdigest() != entry.get("sha256"):
                raise L2AuditError("Recorded message hash mismatch")
            payload_size += len(message_bytes)
            digest.update(message_bytes + b"\n")
            recv = _utc(entry.get("received_utc"))
            monotonic = entry.get("local_monotonic_ns")
            if (
                not isinstance(monotonic, str)
                or not monotonic.isdigit()
                or int(monotonic) <= last_ns
            ):
                raise L2AuditError("Local monotonic receive clock reversed")
            last_ns = int(monotonic)
            if last_recv is not None and recv < last_recv:
                raise L2AuditError("Local UTC receive clock reversed")
            if first_recv is None:
                first_recv = recv
            last_recv = recv
            m = _json(text)
            if not isinstance(m, dict):
                raise L2AuditError("WebSocket JSON must be object")
            sq = m.get("sequence_num")
            if type(sq) is not int or sq < 0:
                raise L2AuditError("Missing or invalid sequence number")
            sequence.append(sq)
            ch = m.get("channel")
            if ch not in ("l2_data", "market_trades", "heartbeats", "subscriptions"):
                raise L2AuditError("Unknown WS channel for fixed capture")
            channels[ch] += 1
            _utc(m.get("timestamp"))
            events = m.get("events")
            if not isinstance(events, list):
                raise L2AuditError("No event array")
            for e in events:
                if not isinstance(e, dict):
                    raise L2AuditError("Event must be object")
                if ch == "l2_data":
                    p = e.get("product_id")
                    kind = e.get("type")
                    if p not in expected or kind not in ("snapshot", "update"):
                        raise L2AuditError("Unknown L2 product or event")
                    if kind == "snapshot":
                        if snapshot_counts[p]:
                            raise L2AuditError("Unexpected duplicate full snapshot")
                        books[p] = {"bid": {}, "offer": {}}
                        snapshot_counts[p] += 1
                    elif not snapshot_counts[p]:
                        raise L2AuditError(
                            "L2 incremental update arrived before snapshot"
                        )
                    else:
                        update_counts[p] += 1
                    changes = e.get("updates")
                    if not isinstance(changes, list):
                        raise L2AuditError("Invalid L2 update list")
                    for u in changes:
                        if not isinstance(u, dict) or u.get("side") not in (
                            "bid",
                            "offer",
                        ):
                            raise L2AuditError("Unknown book side")
                        side = u["side"]
                        price = _decimal(u.get("price_level"))
                        quantity = _decimal(u.get("new_quantity"), zero_ok=True)
                        _utc(u.get("event_time"))
                        if quantity == 0:
                            books[p][side].pop(price, None)
                        else:
                            books[p][side][price] = quantity
                        side_updates[(p, side)] += 1
                        if kind == "snapshot":
                            snapshot_levels[p] += 1
                    if kind == "update" and books[p]["bid"] and books[p]["offer"]:
                        # A positive spread is a useful, limited internal sanity invariant.
                        # This is not independent verification against source exchange
                        # state.
                        if max(books[p]["bid"]) >= min(books[p]["offer"]):
                            invalid_books += 1
                elif ch == "market_trades":
                    trades = e.get("trades")
                    if not isinstance(trades, list) or e.get("type") not in (
                        "snapshot",
                        "update",
                    ):
                        raise L2AuditError("Invalid market-trades event")
                    for trade in trades:
                        if (
                            not isinstance(trade, dict)
                            or trade.get("product_id") not in expected
                        ):
                            raise L2AuditError("Unknown trade product")
                        if trade.get("side") not in ("BUY", "SELL"):
                            raise L2AuditError("Unknown trade side")
                        _decimal(trade.get("price"))
                        _decimal(trade.get("size"))
                        _utc(trade.get("time"))
                        if not isinstance(trade.get("trade_id"), str):
                            raise L2AuditError("Missing source trade identifier")
                        trade_key = (trade["product_id"], trade["trade_id"])
                        fingerprint = (
                            e["type"],
                            trade["side"],
                            _decimal(trade["price"]),
                            _decimal(trade["size"]),
                            _utc(trade["time"]),
                            frozenset(trade.items()),
                        )
                        if trade_key in observed_trade_ids:
                            if (
                                not allow_exact_trade_replays
                                or e["type"] != "update"
                                or observed_trade_ids[trade_key] != fingerprint
                            ):
                                raise L2AuditError(
                                    "Duplicate or contradictory trade identifier"
                                )
                            duplicate_identical_trade_updates[trade["product_id"]] += 1
                            continue
                        observed_trade_ids[trade_key] = fingerprint
                        trade_records[trade["product_id"]] += 1
                        trade_by_type[(trade["product_id"], e["type"])] += 1
            rows += 1
    if (
        not rows
        or rows != status.get("records")
        or payload_size != status.get("payload_bytes")
    ):
        raise L2AuditError("Status and raw message totals disagree")
    if digest.hexdigest() != status.get("stream_hash"):
        raise L2AuditError("Whole-session raw-text payload stream hash mismatch")
    if set(snapshot_counts) != set(expected) or any(
        snapshot_counts[p] != 1 for p in expected
    ):
        raise L2AuditError("Both market initial snapshots were not received")
    if not all(update_counts[p] > 0 for p in expected):
        raise L2AuditError("Expected at least one incremental event per market")
    if channels != Counter(status.get("channel_count")):
        raise L2AuditError("Recorded channel message totals differ")
    # In THIS one captured WS connection the sequence numbering spans all
    # channels and products; do not incorrectly detect gaps per product by
    # filtering other-channel messages away.
    gap_count = sum(b != a + 1 for a, b in zip(sequence, sequence[1:]))
    if len(sequence) != len(set(sequence)):
        raise L2AuditError("Duplicate connection message sequence numbers")
    if gap_count:
        raise L2AuditError("Gap or reorder in connection sequence; book not admissible")
    if invalid_books:
        raise L2AuditError("Crossed/locked reconstructed book during session")
    end = {}
    for p in expected:
        bid = books[p]["bid"]
        ask = books[p]["offer"]
        if not bid or not ask or max(bid) >= min(ask):
            raise L2AuditError("Final book has no positive spread")
        end[p] = {
            "snapshot_events": snapshot_counts[p],
            "incremental_events": update_counts[p],
            "initial_snapshot_price_level_updates": snapshot_levels[p],
            "bid_price_levels_final": len(bid),
            "ask_price_levels_final": len(ask),
            "best_bid_final": str(max(bid)),
            "best_ask_final": str(min(ask)),
            "individual_trades_reported": trade_records[p],
            "unique_trade_ids": trade_records[p],
            "identical_update_replays_excluded": duplicate_identical_trade_updates[p],
            "trade_records_initial_snapshot": trade_by_type[(p, "snapshot")],
            "trade_records_incremental_updates": trade_by_type[(p, "update")],
        }
    return {
        "status": "SOURCE_TEXT_AND_SHORT_L2_STATE_INTERNAL_AUDIT_PASSED",
        "capture_type": "SINGLE_PUBLIC_SPOT_SESSION_ONLY",
        "exact_trade_update_replay_policy_enabled": allow_exact_trade_replays,
        "identical_trade_replays_excluded": dict(duplicate_identical_trade_updates),
        "received_message_count": rows,
        "utf8_payload_bytes": payload_size,
        "source_text_stream_sha256": digest.hexdigest(),
        "observed_connection_sequence_first": sequence[0],
        "observed_connection_sequence_last": sequence[-1],
        "observed_connection_sequence_nonunit_increments": gap_count,
        "observed_connection_sequence_global_across_all_channels": True,
        "channels": dict(channels),
        "received_utc_first": first_recv.isoformat(),
        "received_utc_last": last_recv.isoformat(),
        "per_product": end,
        "fee_funding_spread_execution_cost_evaluated": False,
        "independent_source_reference_book_verified": False,
        "long_duration_disconnection_recovery_verified": False,
        "source_authenticity_independently_verified": False,
        "observable_receipt_time_is_on_github_runner_only": True,
        "trades_from_initial_snapshot_are_not_new_post_subscription_fills": True,
        "alpha_verified": False,
        "orders_enabled": False,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("folder", type=Path)
    p.add_argument(
        "--allow-exact-trade-replays",
        action="store_true",
        help="Explicitly permit exact repeated updates, never conflicting trade IDs",
    )
    args = p.parse_args()
    print(
        json.dumps(
            audit_session(
                args.folder, allow_exact_trade_replays=args.allow_exact_trade_replays
            ),
            indent=2,
            sort_keys=True,
        )
    )
