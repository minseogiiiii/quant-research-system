"""Historical observed trade flow; one admitted source epoch, no execution claims.

Coinbase maker side is inverted to infer aggressive side. Receiver-time buckets
are not exchange-time buckets. CVD is anchored at the requested window start.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from f4_depth_buckets import units, decimal
from independent_soak_replay import replay, strict_json
from v35_archive_ingress import verify_capture, _utc_ms

PRODUCTS = ("BTC-USD", "ETH-USD")
MAX_TRADES = 500_000
MAX_CELLS = 100_000


def signed(n):
    return ("-" if n < 0 else "") + decimal(abs(n))


def aggregate(packets, *, product, start_ms, end_ms, bucket_ms, price_width):
    if product not in PRODUCTS or not all(type(t) is int for t in (start_ms,end_ms,bucket_ms)):
        raise ValueError("F4_FLOW_DOMAIN")
    if not (0 < start_ms < end_ms < 2**53 and 100 <= bucket_ms <= 60_000
            and start_ms % bucket_ms == end_ms % bucket_ms == 0
            and (end_ms-start_ms)//bucket_ms <= 2000):
        raise ValueError("F4_FLOW_TIME_GRID")
    width = units(price_width)
    if width <= 0:
        raise ValueError("F4_FLOW_PRICE_GRID")
    seen, bars, cells = {}, {}, {}
    snapshots = duplicates = unique = 0
    first = last = None
    prior = -1
    for seq, t, wire in packets:
        if type(seq) is not int or seq != prior+1 or type(t) is not int or t <= 0 or (last is not None and t < last):
            raise ValueError("F4_FLOW_SEQUENCE")
        prior = seq
        first = t if first is None else first
        last = t
        if wire["channel"] != "market_trades":
            continue
        for event in wire["events"]:
            kind = event["type"]
            if kind not in ("snapshot", "update"):
                raise ValueError("F4_FLOW_KIND")
            for trade in event["trades"]:
                if trade["product_id"] not in PRODUCTS:
                    raise ValueError("F4_FLOW_PRODUCT")
                if trade["product_id"] != product:
                    continue
                tid, side = trade["trade_id"], trade["side"]
                px, qty = units(trade["price"]), units(trade["size"])
                if not isinstance(tid,str) or not 0 < len(tid) <= 128 or side not in ("BUY","SELL") or not px or not qty:
                    raise ValueError("F4_FLOW_TRADE")
                identity = (kind, px, qty, side, trade["time"])
                if tid in seen:
                    old, old_seq = seen[tid]
                    if old != identity or old_seq == seq or kind != "update":
                        raise ValueError("F4_FLOW_CONFLICT")
                    duplicates += 1
                    continue
                if len(seen) >= MAX_TRADES:
                    raise ValueError("F4_FLOW_TRADE_BOUND")
                seen[tid] = (identity, seq)
                if kind == "snapshot":
                    snapshots += 1
                    continue
                if not start_ms <= t < end_ms:
                    continue
                unique += 1
                bucket = t//bucket_ms*bucket_ms
                bin_low = px//width*width
                # SELL maker is an aggressive buy; BUY maker is aggressive sell.
                index = 0 if side == "SELL" else 1
                bar = bars.setdefault(bucket,[0,0,0,t,t])
                bar[index] += qty
                bar[2] += 1
                bar[4] = t
                k = (bucket,bin_low)
                cell = cells.setdefault(k,[0,0,0])
                if len(cells) > MAX_CELLS:
                    raise ValueError("F4_FLOW_CELL_BOUND")
                cell[index] += qty
                cell[2] += 1
    if first is None:
        raise ValueError("F4_FLOW_EMPTY_SOURCE")
    grouped = {}
    for (t,p),(buy,sell,n) in sorted(cells.items()):
        grouped.setdefault(t,[]).append({"price_low":decimal(p),"buy_base":decimal(buy),"sell_base":decimal(sell),"trades":n})
    output = []
    cvd = 0
    for t,(buy,sell,n,begin,finish) in sorted(bars.items()):
        cvd += buy-sell
        output.append({"bucket_start_ms":t,"first_trade_receiver_ms":begin,
            "last_trade_receiver_ms":finish,"buy_base":decimal(buy),"sell_base":decimal(sell),
            "delta_base":signed(buy-sell),"observed_cvd_base":signed(cvd),"trades":n,"cells":grouped[t]})
    empty = [t for t in range(start_ms,end_ms,bucket_ms) if t not in bars]
    return {"schema":"f4_observed_order_flow_v1","mode":"ARCHIVE_REPLAY","product":product,
        "time_basis":"RECEIVER_MS","side_basis":"INVERT_COINBASE_MAKER_SIDE",
        "start_ms":start_ms,"end_ms":end_ms,"bucket_ms":bucket_ms,"price_width":decimal(width),
        "source_first_receiver_ms":first,"source_last_receiver_ms":last,
        "unique_window_update_trades":unique,"snapshot_trades_excluded":snapshots,
        "identical_update_replays_excluded":duplicates,"rows":output,
        "buckets_without_observed_trades_ms":empty,
        "cvd_definition":"CUMULATIVE_OBSERVED_SIGNED_BASE_VOLUME_SINCE_REQUEST_START",
        "live_feed":False,"execution_authorized":False,"zero_gap_certified":False,"oos_approved":False}


def read_packets(root, manifest):
    for chunk in manifest["chunks"]:
        path = root / chunk["file"]
        if path.is_symlink() or path.parent.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("F4_FLOW_UNSAFE_CHUNK")
        blob = path.read_bytes()
        if len(blob) != chunk["archived_line_bytes"] or hashlib.sha256(blob).hexdigest() != chunk["archive_sha256"]:
            raise ValueError("F4_FLOW_CHUNK_CHANGED")
        for line in blob.splitlines(keepends=True):
            if not line.endswith(b"\n"):
                raise ValueError("F4_FLOW_TRUNCATED")
            row = strict_json(line)
            raw = row["raw_text"]
            if row["connection_id"] != 0 or hashlib.sha256(raw.encode()).hexdigest() != row["sha256"]:
                raise ValueError("F4_FLOW_ENVELOPE")
            wire = strict_json(raw)
            yield wire["sequence_num"], _utc_ms(row["received_utc"]), wire


def build(root, pin, **kwargs):
    root = Path(root)
    cert = verify_capture(root,pin)
    if not cert["source_eligible_for_state_replay"] or cert["connections"] != 1:
        raise ValueError("F4_FLOW_SINGLE_ADMITTED_EPOCH")
    raw = (root/"manifest.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != pin:
        raise ValueError("F4_FLOW_MANIFEST_CHANGED")
    witness = replay(root)
    if witness["messages"] != cert["messages"] or witness["connections"] != 1:
        raise ValueError("F4_FLOW_WITNESS_MISMATCH")
    manifest = strict_json(raw)
    out = aggregate(read_packets(root,manifest),**kwargs)
    if (root/"manifest.json").read_bytes() != raw:
        raise ValueError("F4_FLOW_MANIFEST_CHANGED")
    out.update(source_manifest_sha256=pin,epoch=pin+":0",source_messages=cert["messages"],
               independent_replay_status=witness["status"])
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("archive",type=Path)
    p.add_argument("--manifest-sha256",required=True)
    p.add_argument("--product",choices=PRODUCTS,required=True)
    for name in ("start-ms","end-ms","bucket-ms"):
        p.add_argument("--"+name,type=int,required=True)
    p.add_argument("--price-width",required=True)
    p.add_argument("--output",type=Path,required=True)
    args = vars(p.parse_args())
    root,pin,out = (args.pop(k) for k in ("archive","manifest_sha256","output"))
    result = build(root,pin,**args)
    with out.open("x") as stream:
        json.dump(result,stream,sort_keys=True,separators=(",",":"))
        stream.write("\n")


if __name__ == "__main__":
    main()
