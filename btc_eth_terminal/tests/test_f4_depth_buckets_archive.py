"""Archive-envelope integration with a separate exact Decimal bucket witness.

Generated fixture only: no claim of real Coinbase evidence.
"""

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from f4_depth_buckets import build
from v35_archive_ingress import ArchiveRejected

BASE = 1_800_000_000_000


def stamp(ms):
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def level(side, price, qty, t):
    return dict(side=side, price_level=price, new_quantity=qty, event_time=stamp(t))


def write_archive(root):
    (root / "chunks").mkdir()
    packets = []

    def event(t, p, kind, levels):
        event = dict(type=kind, product_id=p, updates=[level(*x, t) for x in levels])
        raw = json.dumps(
            dict(
                channel="l2_data",
                timestamp=stamp(t),
                sequence_num=len(packets),
                events=[event],
            ),
            separators=(",", ":"),
        )
        packets.append(
            dict(
                connection_id=0,
                received_utc=stamp(t),
                local_monotonic_ns=str(len(packets) + 1),
                raw_text=raw,
                sha256=hashlib.sha256(raw.encode()).hexdigest(),
            )
        )

    event(
        BASE + 100,
        "BTC-USD",
        "snapshot",
        [("bid", "100", "0.1"), ("bid", "101", "0.2"), ("offer", "110", "3")],
    )
    event(BASE + 200, "ETH-USD", "snapshot", [("bid", "20", "4"), ("offer", "21", "2")])
    event(BASE + 300, "BTC-USD", "update", [("bid", "101", "0.4")])
    event(BASE + 1100, "BTC-USD", "update", [("bid", "100", "0"), ("bid", "99", "0.7")])
    event(BASE + 3100, "BTC-USD", "update", [("offer", "111", "0.001")])
    filename = "chunks/chunk_00000_conn_000.jsonl"
    blob = b"".join(
        (json.dumps(p, separators=(",", ":")) + "\n").encode() for p in packets
    )
    (root / filename).write_bytes(blob)
    raw_bytes = sum(len(p["raw_text"].encode()) for p in packets)
    manifest = dict(
        format="btc_eth_raw_soak_v10",
        source="wss://advanced-trade-ws.coinbase.com",
        source_authentication="PUBLIC_WEBSOCKET_NO_SIGNED_EXCHANGE_ATTESTATION",
        products=["BTC-USD", "ETH-USD"],
        sequence_scope="PER_WEBSOCKET_CONNECTION",
        orders_enabled=False,
        strategy_execution_enabled=False,
        point_in_time_historical_availability_verified=False,
        executable_alpha_verified=False,
        ready_for_research=True,
        status="CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH",
        complete_sequence_per_connection=True,
        all_connections_have_snapshots=True,
        faults=[],
        chunks=[
            dict(
                file=filename,
                connection_id=0,
                messages=len(packets),
                archived_line_bytes=len(blob),
                raw_text_bytes=raw_bytes,
                archive_sha256=hashlib.sha256(blob).hexdigest(),
                first_sequence=0,
                last_sequence=len(packets) - 1,
                first_received_utc=packets[0]["received_utc"],
                last_received_utc=packets[-1]["received_utc"],
            )
        ],
        connections=[
            dict(
                id=0,
                messages=len(packets),
                last_sequence=len(packets) - 1,
                snapshots={"BTC-USD": True, "ETH-USD": True},
                integrity_fault=False,
                updates={"BTC-USD": 3, "ETH-USD": 0},
                heartbeat_count=0,
            )
        ],
        messages=len(packets),
        bytes=raw_bytes,
    )
    data = json.dumps(manifest).encode()
    (root / "manifest.json").write_bytes(data)
    return hashlib.sha256(data).hexdigest(), packets


def decimal_witness(packets):
    # Replay each occupied time bucket independently from the start; intentionally
    # different algorithm from the incremental fixed-point implementation.
    expected = {}
    for bucket in (0, 1000, 3000):
        book = {}
        for packet in packets:
            t = int(datetime.fromisoformat(packet["received_utc"]).timestamp() * 1000)
            if t >= BASE + bucket + 1000:
                break
            for ev in json.loads(packet["raw_text"])["events"]:
                if ev["product_id"] != "BTC-USD":
                    continue
                for u in ev["updates"]:
                    k = (u["side"], Decimal(u["price_level"]))
                    q = Decimal(u["new_quantity"])
                    if q:
                        book[k] = q
                    else:
                        book.pop(k, None)
        totals = {}
        for (side, price), qty in book.items():
            if Decimal(90) <= price < Decimal(120):
                k = (side, price // Decimal(10) * 10)
                totals[k] = totals.get(k, Decimal(0)) + qty
        expected[BASE + bucket] = totals
    return expected


class ArchiveIntegration(unittest.TestCase):
    def test_end_to_end_and_independent_decimal_buckets(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pin, packets = write_archive(root)
            r = build(
                root,
                pin,
                product="BTC-USD",
                start_ms=BASE,
                end_ms=BASE + 4000,
                bucket_ms=1000,
                low="90",
                high="120",
                width="10",
            )
            observed = {
                row["bucket_start_ms"]: {
                    (c["side"], Decimal(c["price_low"])): Decimal(c["quantity"])
                    for c in row["cells"]
                }
                for row in r["rows"]
            }
            self.assertEqual(observed, decimal_witness(packets))
            self.assertEqual(r["missing_buckets_ms"], [BASE + 2000])
            self.assertEqual(r["source_manifest_sha256"], pin)
            self.assertEqual(r["source_messages"], 5)
            self.assertEqual(
                r["independent_replay_status"], "INDEPENDENT_SOURCE_REPLAY_ONLY"
            )
            self.assertFalse(r["live_feed"])

    def test_modified_chunk_fails_before_projection(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pin, _ = write_archive(root)
            chunk = next((root / "chunks").glob("*.jsonl"))
            chunk.write_bytes(chunk.read_bytes() + b" ")
            with self.assertRaises(ArchiveRejected):
                build(
                    root,
                    pin,
                    product="BTC-USD",
                    start_ms=BASE,
                    end_ms=BASE + 4000,
                    bucket_ms=1000,
                    low="90",
                    high="120",
                    width="10",
                )


if __name__ == "__main__":
    unittest.main()
