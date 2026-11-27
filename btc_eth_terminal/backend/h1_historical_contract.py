"""Versioned historical packet contract. Never equate venue time and receipt time."""

from __future__ import annotations
import hashlib
import json
from f5_independent_clock import milliseconds
from f4_depth_buckets import units

PRODUCTS = ("BTC-USD", "ETH-USD")
CHANNELS = ("l2_data", "market_trades", "subscriptions", "heartbeats")


def need(ok, reason):
    if not ok:
        raise ValueError("H1_" + reason)


def normalized(row, wire, pin, chunk):
    seq = wire["sequence_num"]
    cid = row["connection_id"]
    channel = wire["channel"]
    need(
        type(seq) is int
        and seq >= 0
        and type(cid) is int
        and cid == 0
        and channel in CHANNELS,
        "PACKET",
    )
    mono = row["local_monotonic_ns"]
    need(
        type(mono) is str and mono.isascii() and mono.isdecimal() and int(mono) < 2**63,
        "MONOTONIC",
    )
    raw = row["raw_text"]
    need(hashlib.sha256(raw.encode()).hexdigest() == row["sha256"], "PAYLOAD_HASH")
    received = milliseconds(row["received_utc"])
    packet = dict(
        source_manifest_sha256=pin,
        epoch=pin + ":0",
        venue="COINBASE_ADVANCED",
        connection_id=cid,
        sequence=seq,
        receiver_ms=received,
        receiver_utc=row["received_utc"],
        receiver_monotonic_ns=int(mono),
        channel=channel,
        chunk=chunk,
        payload_sha256=row["sha256"],
        payload=raw,
    )
    levels = []
    trades = []
    for i, e in enumerate(wire["events"]):
        if channel == "l2_data":
            need(
                e["product_id"] in PRODUCTS and e["type"] in ("snapshot", "update"),
                "L2_EVENT",
            )
            for j, u in enumerate(e["updates"]):
                p, q = units(u["price_level"]), units(u["new_quantity"])
                need(p > 0 and u["side"] in ("bid", "offer"), "L2_LEVEL")
                levels.append(
                    dict(
                        sequence=seq,
                        event_index=i,
                        item_index=j,
                        receiver_ms=received,
                        product=e["product_id"],
                        kind=e["type"],
                        side=u["side"],
                        price=u["price_level"],
                        quantity=u["new_quantity"],
                        venue_event_utc=u.get("event_time"),
                    )
                )
        elif channel == "market_trades":
            need(e["type"] in ("snapshot", "update"), "TRADE_EVENT")
            for j, u in enumerate(e["trades"]):
                need(
                    u["product_id"] in PRODUCTS
                    and u["side"] in ("BUY", "SELL")
                    and units(u["price"]) > 0
                    and units(u["size"]) > 0,
                    "TRADE",
                )
                trades.append(
                    dict(
                        sequence=seq,
                        event_index=i,
                        item_index=j,
                        receiver_ms=received,
                        product=u["product_id"],
                        kind=e["type"],
                        maker_side=u["side"],
                        trade_id=u["trade_id"],
                        price=u["price"],
                        quantity=u["size"],
                        venue_event_utc=u["time"],
                    )
                )
    return packet, levels, trades
