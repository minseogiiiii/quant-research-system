"""Independent strict replay of a short Coinbase Advanced Trade public L2 recording.

Only internal text/sequence/book invariants are validated. This is NOT an external
venue reference-book reconciliation, trader-observed latency, or a strategy test.
The standalone implementation intentionally does NOT import audit_l2_pilot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

PRODUCTS = ("BTC-USD", "ETH-USD")
CHANNELS = ("l2_data", "market_trades", "heartbeats", "subscriptions")


class StrictL2Error(ValueError):
    """Recording rejected or insufficient to validate the requested invariants."""


def _reject_duplicates(pairs):
    found = {}
    for key, value in pairs:
        if key in found:
            raise StrictL2Error("Duplicate JSON property")
        found[key] = value
    return found


def _reject_constant(value):
    raise StrictL2Error("Nonfinite JSON constant: " + value)


def _json(raw):
    try:
        return json.loads(
            raw, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant
        )
    except (ValueError, TypeError, UnicodeError) as exc:
        raise StrictL2Error("Malformed or ambiguous JSON") from exc


def _time(v):
    if not isinstance(v, str):
        raise StrictL2Error("Timestamp must be UTC string")
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except (ValueError, OverflowError) as exc:
        raise StrictL2Error("Invalid timestamp") from exc
    if t.tzinfo is None or t.utcoffset() is None or t.utcoffset().total_seconds() != 0:
        raise StrictL2Error("Timestamp is not UTC")
    return t


def _number(v, *, allow_zero=False):
    if not isinstance(v, str) or not v.strip():
        raise StrictL2Error("Expected decimal string")
    try:
        n = Decimal(v)
    except InvalidOperation as exc:
        raise StrictL2Error("Invalid decimal") from exc
    if not n.is_finite() or n < 0 or (n == 0 and not allow_zero):
        raise StrictL2Error("Nonfinite or invalid nonpositive market amount")
    return n


def _stats(values):
    if not values:
        return None
    a = sorted(values)
    return {
        "count": len(a),
        "min": round(a[0], 3),
        "median": round(statistics.median(a), 3),
        "p95_observed": round(a[(95 * (len(a) - 1)) // 100], 3),
        "max": round(a[-1], 3),
    }


def _state_hash(book):
    digest = hashlib.sha256()
    for side in ("bid", "offer"):
        for price, quantity in sorted(book[side].items()):
            digest.update(
                (side + ":" + str(price) + ":" + str(quantity) + "\n").encode()
            )
    return digest.hexdigest()


def deep_replay(folder: Path, *, allow_exact_trade_replays: bool = False) -> dict:
    folder = Path(folder).resolve()
    status_file = folder / "capture_status.json"
    stream_file = folder / "raw_websocket_payloads.jsonl"
    for p in (status_file, stream_file):
        if not p.is_file() or p.is_symlink() or p.resolve().parent != folder:
            raise StrictL2Error("Recording missing or symlinked")
    if not (0 < stream_file.stat().st_size < 40_000_000):
        raise StrictL2Error("Recording outside bounded sample size")
    status = _json(status_file.read_bytes())
    if not isinstance(status, dict):
        raise StrictL2Error("Invalid capture status")
    if (
        status.get("status") != "PUBLIC_L2_TEXT_CAPTURED_NOT_RECONSTRUCTED"
        or status.get("endpoint") != "wss://advanced-trade-ws.coinbase.com"
        or status.get("products") != list(PRODUCTS)
        or status.get("orders_enabled") is not False
        or status.get("hash_is_of_utf8_text_not_wire_frames") is not True
        or status.get("error_count") != 0
        or status.get("closed_early") is not False
    ):
        raise StrictL2Error("Wrong capture source, scope or declared completeness")
    start = _time(status.get("start_utc"))
    finish = _time(status.get("end_utc"))
    if not start < finish:
        raise StrictL2Error("Invalid session window")
    books = {p: {"bid": {}, "offer": {}} for p in PRODUCTS}
    channels = Counter()
    snapshot_counts = Counter()
    update_counts = Counter()
    snapshot_levels = Counter()
    update_levels = Counter()
    trade_counts = Counter()
    trade_ids = {p: {} for p in PRODUCTS}
    replayed_identical_trades = Counter()
    snapshot_trade_counts = Counter()
    live_trade_counts = Counter()
    trade_sample_order_regressions = Counter()
    last_trade_time = {}
    event_lags = {p: [] for p in PRODUCTS}
    packet_lags = []
    spreads = {p: [] for p in PRODUCTS}
    heartbeat_counter = None
    heartbeats = 0
    subscription_ack_count = 0
    ack_channels = set()
    previous_seq = None
    last_ns = -1
    last_receive = None
    last_l2_time = {}
    regression_counts = Counter()
    raw_hash = hashlib.sha256()
    raw_bytes = 0
    total = 0
    rejected_crossed = 0
    with stream_file.open("rb") as src:
        for index, line in enumerate(src):
            if not line.endswith(b"\n") or len(line) > 14_000_000:
                raise StrictL2Error("Truncated or oversized recording row")
            row = _json(line)
            if not isinstance(row, dict) or not isinstance(row.get("raw_text"), str):
                raise StrictL2Error("Bad source envelope")
            payload = row["raw_text"].encode("utf-8")
            if hashlib.sha256(payload).hexdigest() != row.get("sha256"):
                raise StrictL2Error("Message-level SHA mismatch")
            raw_hash.update(payload + b"\n")
            raw_bytes += len(payload)
            recv = _time(row.get("received_utc"))
            mono = row.get("local_monotonic_ns")
            if not isinstance(mono, str) or not mono.isdigit() or int(mono) <= last_ns:
                raise StrictL2Error("Monotonic local receive clock regressed")
            last_ns = int(mono)
            if last_receive and recv < last_receive:
                raise StrictL2Error("UTC receive clock regressed")
            if recv < start or recv > finish:
                raise StrictL2Error("Message received outside declared capture window")
            last_receive = recv
            m = _json(payload)
            if not isinstance(m, dict):
                raise StrictL2Error("Invalid market-data message")
            seq = m.get("sequence_num")
            if type(seq) is not int or seq < 0:
                raise StrictL2Error("Invalid connection sequence number")
            if previous_seq is not None and seq != previous_seq + 1:
                raise StrictL2Error(
                    "Connection sequence GAP/REORDER at message " + str(index)
                )
            previous_seq = seq
            ch = m.get("channel")
            if ch not in CHANNELS:
                raise StrictL2Error("Unexpected subscription channel")
            channels[ch] += 1
            total += 1
            server_time = _time(m.get("timestamp"))
            packet_lags.append((recv - server_time).total_seconds() * 1000)
            if (server_time - recv).total_seconds() > 30 or (
                recv - server_time
            ).total_seconds() > 120:
                raise StrictL2Error(
                    "Feed envelope timestamp too far from local receive clock"
                )
            events = m.get("events")
            if not isinstance(events, list):
                raise StrictL2Error("Missing events")
            for event in events:
                if not isinstance(event, dict):
                    raise StrictL2Error("Malformed event")
                if ch == "l2_data":
                    product = event.get("product_id")
                    kind = event.get("type")
                    if product not in PRODUCTS or kind not in ("snapshot", "update"):
                        raise StrictL2Error("Unknown product or book event")
                    if kind == "snapshot":
                        if snapshot_counts[product]:
                            raise StrictL2Error("Duplicate initial book snapshot")
                        snapshot_counts[product] += 1
                        books[product] = {"bid": {}, "offer": {}}
                    elif not snapshot_counts[product]:
                        raise StrictL2Error("Incremental level2 before snapshot")
                    else:
                        update_counts[product] += 1
                    ups = event.get("updates")
                    if not isinstance(ups, list):
                        raise StrictL2Error("Bad L2 changes")
                    for change in ups:
                        if not isinstance(change, dict) or change.get("side") not in (
                            "bid",
                            "offer",
                        ):
                            raise StrictL2Error("Unexpected book side")
                        p = _number(change.get("price_level"))
                        q = _number(change.get("new_quantity"), allow_zero=True)
                        event_time = _time(change.get("event_time"))
                        if (
                            kind == "update"
                            and (event_time - recv).total_seconds() > 30
                        ):
                            raise StrictL2Error("L2 event timestamp far in the future")
                        if kind == "update":
                            event_lags[product].append(
                                (recv - event_time).total_seconds() * 1000
                            )
                            if (
                                product in last_l2_time
                                and event_time < last_l2_time[product]
                            ):
                                regression_counts[product] += 1
                            last_l2_time[product] = event_time
                            update_levels[product] += 1
                        else:
                            snapshot_levels[product] += 1
                        side = books[product][change["side"]]
                        if not q:
                            side.pop(p, None)
                        else:
                            side[p] = q
                    if books[product]["bid"] and books[product]["offer"]:
                        best_bid = max(books[product]["bid"])
                        best_ask = min(books[product]["offer"])
                        if best_bid >= best_ask:
                            rejected_crossed += 1
                        if kind == "update":
                            spreads[product].append(
                                float(
                                    (best_ask - best_bid)
                                    / ((best_bid + best_ask) / 2)
                                    * 10000
                                )
                            )
                elif ch == "market_trades":
                    if event.get("type") not in (
                        "snapshot",
                        "update",
                    ) or not isinstance(event.get("trades"), list):
                        raise StrictL2Error("Bad public trade envelope")
                    for trade in event["trades"]:
                        if (
                            not isinstance(trade, dict)
                            or trade.get("product_id") not in PRODUCTS
                        ):
                            raise StrictL2Error("Bad public trade product")
                        product = trade["product_id"]
                        identifier = trade.get("trade_id")
                        if not isinstance(identifier, str) or not identifier.strip():
                            raise StrictL2Error("Missing/invalid trade identifier")
                        if trade.get("side") not in ("BUY", "SELL"):
                            raise StrictL2Error("Unknown source-reported trade side")
                        trade_price = _number(trade.get("price"))
                        trade_size = _number(trade.get("size"))
                        t = _time(trade.get("time"))
                        # Trading messages may replay the very same *update* trade.
                        # Duplicate IDs with different economics or a snapshot/update
                        # identity collision must still fail closed.
                        fingerprint = (
                            event["type"],
                            trade["side"],
                            trade_price,
                            trade_size,
                            t,
                            frozenset(trade.items()),
                        )
                        if identifier in trade_ids[product]:
                            if (
                                not allow_exact_trade_replays
                                or event["type"] != "update"
                                or trade_ids[product][identifier] != fingerprint
                            ):
                                raise StrictL2Error(
                                    "Conflicting/repeated trade identifier"
                                )
                            replayed_identical_trades[product] += 1
                            continue
                        trade_ids[product][identifier] = fingerprint
                        trade_counts[product] += 1
                        if (t - recv).total_seconds() > 30:
                            raise StrictL2Error(
                                "Trade event timestamp far in the future"
                            )
                        if product in last_trade_time and t < last_trade_time[product]:
                            trade_sample_order_regressions[product] += 1
                        last_trade_time[product] = t
                        if event["type"] == "snapshot":
                            snapshot_trade_counts[product] += 1
                        else:
                            live_trade_counts[product] += 1
                elif ch == "subscriptions":
                    sub = event.get("subscriptions")
                    if not isinstance(sub, dict):
                        raise StrictL2Error("Malformed subscription acknowledgment")
                    for k, v in sub.items():
                        if k not in ("level2", "market_trades", "heartbeats"):
                            raise StrictL2Error(
                                "Unexpected subscription channel acknowledgment"
                            )
                        if not isinstance(v, list):
                            raise StrictL2Error(
                                "Subscription acknowledgment must be list"
                            )
                        if k == "heartbeats":
                            if v != ["heartbeats"]:
                                raise StrictL2Error(
                                    "Missing heartbeat subscription acknowledgment"
                                )
                        elif set(v) != set(PRODUCTS) or len(v) != 2:
                            raise StrictL2Error("Subscription market pair mismatch")
                        ack_channels.add(k)
                    subscription_ack_count += 1
                elif ch == "heartbeats":
                    count = event.get("heartbeat_counter")
                    if type(count) is not int or count < 0:
                        raise StrictL2Error("Missing heartbeat counter")
                    if heartbeat_counter is not None and count != heartbeat_counter + 1:
                        raise StrictL2Error("Heartbeat counter GAP/REORDER")
                    heartbeat_counter = count
                    heartbeats += 1
    if (
        total != status.get("records")
        or raw_bytes != status.get("payload_bytes")
        or raw_hash.hexdigest() != status.get("stream_hash")
    ):
        raise StrictL2Error("Session status and payload source disagree")
    if channels != Counter(status.get("channel_count")):
        raise StrictL2Error("Channel totals disagree")
    if (
        heartbeats < 1
        or subscription_ack_count < 1
        or ack_channels != set(("level2", "market_trades", "heartbeats"))
    ):
        raise StrictL2Error(
            "Incomplete subscription acknowledgments or heartbeat evidence"
        )
    if rejected_crossed:
        raise StrictL2Error("Locked/crossed book observed during replay")
    products = {}
    for p in PRODUCTS:
        if snapshot_counts[p] != 1 or update_counts[p] < 1:
            raise StrictL2Error("Missing book snapshot or update")
        bids = books[p]["bid"]
        asks = books[p]["offer"]
        if not bids or not asks or max(bids) >= min(asks):
            raise StrictL2Error("Invalid final spread")
        products[p] = {
            "snapshot_count": snapshot_counts[p],
            "incremental_update_events": update_counts[p],
            "initial_snapshot_level_updates": snapshot_levels[p],
            "incremental_level_updates": update_levels[p],
            "best_bid_final": str(max(bids)),
            "best_ask_final": str(min(asks)),
            "final_bid_price_levels": len(bids),
            "final_ask_price_levels": len(asks),
            "deterministic_book_sha256": _state_hash(books[p]),
            "source_reported_trades_total": trade_counts[p],
            "unique_trade_id_count": trade_counts[p],
            "identical_update_trade_replays_excluded": replayed_identical_trades[p],
            "initial_trade_snapshot_records_not_post_subscription_fills": snapshot_trade_counts[
                p
            ],
            "incremental_trade_records": live_trade_counts[p],
            "trade_messages_may_not_be_sorted_by_trade_time": bool(
                trade_sample_order_regressions[p]
            ),
            "trade_time_regressions_in_message_order": trade_sample_order_regressions[
                p
            ],
            "l2_event_time_regressions_in_message_order": regression_counts[p],
            "observed_l2_update_source_to_runner_receive_ms": _stats(event_lags[p]),
            "observed_update_spread_bp": _stats(spreads[p]),
        }
    # The original 20s producer counted trades at the envelope level. In this
    # real feed product_id lives on each trade record, so producer count = 0.
    # This is a producer TELEMETRY BUG, not a failure of archived message bytes.
    telemetry = {}
    for p in PRODUCTS:
        declared = status.get("counts", {}).get(p, {}).get("trades")
        telemetry[p] = {
            "producer_reported_trades": declared,
            "replayed_trades": trade_counts[p],
            "raw_reported_trade_records": trade_counts[p]
            + replayed_identical_trades[p],
            "capture_telemetry_mismatch": declared
            != (trade_counts[p] + replayed_identical_trades[p]),
        }
    return {
        "status": "INTERNAL_REPLAY_PASSED_NOT_INDEPENDENT_BOOK_REFERENCE",
        "input_stream_sha256": raw_hash.hexdigest(),
        "total_messages": total,
        "connection_sequence_first": 0
        if previous_seq is None
        else previous_seq - total + 1,
        "connection_sequence_last": previous_seq,
        "continuity": "no observed gaps in this single TCP/WebSocket connection",
        "heartbeat_events": heartbeats,
        "exact_trade_update_replay_policy_enabled": allow_exact_trade_replays,
        "identical_trade_replays_excluded": dict(replayed_identical_trades),
        "subscription_ack_events": subscription_ack_count,
        "channel_message_counts": dict(channels),
        "observed_server_timestamp_to_runner_receive_ms": _stats(packet_lags),
        "per_product": products,
        "collector_telemetry": telemetry,
        "independent_exchange_reference_book_verified": False,
        "source_clock_synchronized_to_runner_verified": False,
        "historical_point_in_time_availability_verified": False,
        "disconnection_or_recovery_verified": False,
        "trade_executability_verified": False,
        "strategy_alpha_verified": False,
        "no_orders_sent": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--allow-exact-trade-replays",
        action="store_true",
        help="Explicitly permit only byte/economically identical duplicate update trade IDs",
    )
    args = parser.parse_args()
    result = deep_replay(
        args.folder, allow_exact_trade_replays=args.allow_exact_trade_replays
    )
    serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(serialized, encoding="utf-8")
    print(serialized, end="")


if __name__ == "__main__":
    main()
