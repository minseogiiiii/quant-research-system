"""Independent Decimal witness on generated, sealed Coinbase-shaped input."""

import hashlib
import json
import tempfile
import unittest
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from f4_order_flow import build
from test_f4_depth_buckets_archive import BASE, stamp, write_archive
from v35_archive_ingress import ArchiveRejected


def source(root):
    _, packets = write_archive(root)

    def trade(tid, side, size, price, product="BTC-USD"):
        return dict(
            trade_id=tid,
            side=side,
            size=size,
            price=price,
            product_id=product,
            time=stamp(BASE + 200),
        )

    a = trade("a", "SELL", "0.100000000000000001", "100.3")
    b = trade("b", "BUY", "0.2", "100.9")
    c = trade("c", "SELL", "0.4", "101")
    d = trade("d", "BUY", "0.05", "99.7")
    for t, kind, ts in [
        (BASE + 210, "snapshot", [trade("s", "BUY", "9", "100")]),
        (BASE + 350, "update", [a, b, trade("eth", "BUY", "2", "20", "ETH-USD")]),
        (BASE + 400, "update", [a]),
        (BASE + 1200, "update", [c]),
        (BASE + 3200, "update", [d]),
    ]:
        wire = dict(
            channel="market_trades",
            timestamp=stamp(t),
            events=[dict(type=kind, trades=ts)],
        )
        packets.append(
            dict(connection_id=0, received_utc=stamp(t), raw_text=json.dumps(wire))
        )
    packets.sort(key=lambda p: p["received_utc"])
    for i, p in enumerate(packets):
        wire = json.loads(p["raw_text"])
        wire["sequence_num"] = i
        p["raw_text"] = json.dumps(wire, separators=(",", ":"))
        p["sha256"] = hashlib.sha256(p["raw_text"].encode()).hexdigest()
        p["local_monotonic_ns"] = str(i + 1)
    path = root / "manifest.json"
    m = json.loads(path.read_bytes())
    chunk = m["chunks"][0]
    blob = b"".join(
        (json.dumps(p, separators=(",", ":")) + "\n").encode() for p in packets
    )
    (root / chunk["file"]).write_bytes(blob)
    rawbytes = sum(len(p["raw_text"].encode()) for p in packets)
    chunk.update(
        messages=len(packets),
        archived_line_bytes=len(blob),
        raw_text_bytes=rawbytes,
        archive_sha256=hashlib.sha256(blob).hexdigest(),
        first_sequence=0,
        last_sequence=len(packets) - 1,
        first_received_utc=packets[0]["received_utc"],
        last_received_utc=packets[-1]["received_utc"],
    )
    m.update(messages=len(packets), bytes=rawbytes)
    m["connections"][0].update(messages=len(packets), last_sequence=len(packets) - 1)
    raw = json.dumps(m).encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest(), packets


def witness(packets):
    # Independent algorithm: unique update records then group by Decimal floor
    # and receiver second; no reuse of fixed-point or aggregate helpers.
    trades = {}
    for p in packets:
        wire = json.loads(p["raw_text"])
        if wire["channel"] != "market_trades":
            continue
        for ev in wire["events"]:
            if ev["type"] != "update":
                continue
            for t in ev["trades"]:
                if t["product_id"] == "BTC-USD":
                    trades.setdefault(t["trade_id"], (p["received_utc"], t))
    cells = defaultdict(lambda: [Decimal(0), Decimal(0), 0])
    from datetime import datetime

    for stamp_text, t in trades.values():
        ms = int(datetime.fromisoformat(stamp_text).timestamp() * 1000)
        key = (ms // 1000 * 1000, Decimal(t["price"]) // Decimal(1))
        cell = cells[key]
        cell[0 if t["side"] == "SELL" else 1] += Decimal(t["size"])
        cell[2] += 1
    return dict(cells)


def produce(root):
    pin, packets = source(root)
    return build(
        root,
        pin,
        product="BTC-USD",
        start_ms=BASE,
        end_ms=BASE + 4000,
        bucket_ms=1000,
        price_width="1",
    ), packets


class ArchiveFlow(unittest.TestCase):
    def test_sealed_archive_and_independent_bucket_witness(self):
        with tempfile.TemporaryDirectory() as d:
            report, packets = produce(Path(d))
            cells = {
                (r["bucket_start_ms"], Decimal(c["price_low"])): [
                    Decimal(c["buy_base"]),
                    Decimal(c["sell_base"]),
                    c["trades"],
                ]
                for r in report["rows"]
                for c in r["cells"]
            }
            self.assertEqual(cells, witness(packets))
            self.assertEqual(report["unique_window_update_trades"], 4)
            self.assertEqual(report["snapshot_trades_excluded"], 1)
            self.assertEqual(report["identical_update_replays_excluded"], 1)
            self.assertEqual(
                report["buckets_without_observed_trades_ms"], [BASE + 2000]
            )
            self.assertEqual(
                Decimal(report["rows"][-1]["observed_cvd_base"]),
                Decimal("0.250000000000000001"),
            )
            self.assertFalse(report["live_feed"])

    def test_tamper_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pin, _ = source(root)
            path = next((root / "chunks").glob("*.jsonl"))
            path.write_bytes(path.read_bytes() + b" ")
            with self.assertRaises(ArchiveRejected):
                build(
                    root,
                    pin,
                    product="BTC-USD",
                    start_ms=BASE,
                    end_ms=BASE + 4000,
                    bucket_ms=1000,
                    price_width="1",
                )
