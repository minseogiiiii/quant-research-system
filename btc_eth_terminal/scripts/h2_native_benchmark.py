"""Synthetic native-format H1 throughput benchmark; never market evidence."""

from __future__ import annotations
import argparse
import hashlib
import json
import resource
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from h1_streaming_parquet import convert
from v35_archive_ingress import verify_capture, ArchiveRejected

BASE = 1_800_000_000_000
PRODUCTS = ("BTC-USD", "ETH-USD")
MAX_TARGET = 1_100_000_000
CHUNK = 8_000_000


def need(ok, reason):
    if not ok:
        raise ValueError("H2_BENCH_" + reason)


def encode(x):
    return json.dumps(x, separators=(",", ":"), allow_nan=False).encode()


def stamp(ms):
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def wire(seq, levels, start_ms=BASE):
    t = stamp(start_ms + seq * 10)
    events = []
    for p, base in zip(PRODUCTS, (50000, 2000)):
        rows = []
        for side in ("bid", "offer"):
            for i in range(levels):
                px = base - i if side == "bid" else base + 1 + i
                rows.append(
                    dict(
                        side=side,
                        price_level=str(px),
                        new_quantity=str(1 + (seq + i) % 7) + ".123456789012345678",
                        event_time=t,
                    )
                )
        events.append(
            dict(type="snapshot" if seq == 0 else "update", product_id=p, updates=rows)
        )
    raw = encode(dict(channel="l2_data", timestamp=t, sequence_num=seq, events=events))
    packet = dict(
        connection_id=0,
        received_utc=t,
        local_monotonic_ns=str(seq * 10000000 + 1),
        raw_text=raw.decode(),
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    return encode(packet) + b"\n", len(raw), t


def generate(root, target, levels=128, *, start_ms=BASE):
    need(
        type(target) is int and 1000 <= target <= MAX_TARGET and 2 <= levels <= 256,
        "TARGET",
    )
    need(
        type(start_ms) is int and 1_500_000_000_000 <= start_ms <= 2_000_000_000_000,
        "CLOCK",
    )
    root.mkdir()
    (root / "chunks").mkdir()
    chunks = []
    total = raw_total = seq = 0
    buf = bytearray()
    raw_count = 0
    n = 0
    first = last = None

    def flush():
        nonlocal buf, raw_count, n, first, last
        if not n:
            return
        name = f"chunks/chunk_{len(chunks):05d}_conn_000.jsonl"
        (root / name).write_bytes(buf)
        chunks.append(
            dict(
                file=name,
                connection_id=0,
                messages=n,
                archived_line_bytes=len(buf),
                raw_text_bytes=raw_count,
                archive_sha256=hashlib.sha256(buf).hexdigest(),
                first_sequence=seq - n,
                last_sequence=seq - 1,
                first_received_utc=first,
                last_received_utc=last,
            )
        )
        buf = bytearray()
        raw_count = n = 0
        first = last = None

    while total < target or seq < 2:
        line, size, t = wire(seq, levels, start_ms)
        if n and len(buf) + len(line) > CHUNK:
            flush()
        if n == 0:
            first = t
        last = t
        buf.extend(line)
        raw_count += size
        n += 1
        seq += 1
        total += len(line)
        raw_total += size
    flush()
    m = dict(
        format="btc_eth_raw_soak_v10",
        source="wss://advanced-trade-ws.coinbase.com",
        source_authentication="PUBLIC_WEBSOCKET_NO_SIGNED_EXCHANGE_ATTESTATION",
        products=list(PRODUCTS),
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
        chunks=chunks,
        messages=seq,
        bytes=raw_total,
        connections=[
            dict(
                id=0,
                messages=seq,
                last_sequence=seq - 1,
                snapshots=dict.fromkeys(PRODUCTS, True),
                integrity_fault=False,
                updates=dict.fromkeys(PRODUCTS, seq - 1),
                heartbeat_count=0,
            )
        ],
        synthetic_fixture=True,
        synthetic_fixture_never_admit=True,
        required_duration_seconds=15,
        finish_reason="DURATION_REACHED",
        total_reconnects=0,
    )
    raw = encode(m)
    (root / "manifest.json").write_bytes(raw)
    return hashlib.sha256(raw).hexdigest(), total, seq, len(chunks)


def benchmark(target, root):
    need(
        shutil.disk_usage(root).free >= max(2_000_000_000, target * 5), "DISK_ADMISSION"
    )
    start = time.monotonic()
    raw = root / "raw"
    pin, size, count, chunks = generate(raw, target)
    generate_s = time.monotonic() - start
    legacy_rejected = None
    if size > 1_000_000_000:
        try:
            verify_capture(raw, pin)
        except ArchiveRejected as e:
            # Confirm the size guard, not an unrelated malformed fixture.
            legacy_rejected = str(e)
        need(legacy_rejected is not None, "LEGACY_LIMIT")
    start = time.monotonic()
    timings = {}
    out = root / "dataset"
    r = convert(raw, pin, out, fixture=True, timings=timings)
    elapsed = time.monotonic() - start
    need(
        r["independent_replay_parity"]
        and not r["research_eligible"]
        and r["tables"]["packets"]["rows"] == count
        and r["tables"]["l2"]["rows"] == count * 512,
        "ROUNDTRIP",
    )
    return dict(
        schema="h2_native_synthetic_benchmark_v1",
        status="PASS",
        synthetic=True,
        target_archive_bytes=target,
        actual_archive_bytes=size,
        packets=count,
        chunks=chunks,
        l2_rows=r["tables"]["l2"]["rows"],
        generation_seconds=generate_s,
        conversion_and_audit_seconds=elapsed,
        stage_seconds=timings,
        verification_scan="PHYSICAL_ORDER_AND_CANONICAL_HASH",
        archive_mb_per_second=size / 1e6 / elapsed,
        peak_process_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        parquet_bytes=sum(p.stat().st_size for p in out.glob("*.parquet")),
        legacy_rejection=legacy_rejected,
        source_manifest_sha256=pin,
        independent_replay_parity=True,
        research_eligible=False,
        real_market_benchmark=False,
        oos_approved=False,
        alpha_confirmed=False,
        execution_authorized=False,
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--target-bytes", type=int, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    with tempfile.TemporaryDirectory(prefix="h2-bench-") as d:
        r = benchmark(a.target_bytes, Path(d))
    with a.output.open("x") as f:
        json.dump(r, f, sort_keys=True, allow_nan=False)
        f.write("\n")
    print("H2_NATIVE_SYNTHETIC_BENCHMARK_PASS", flush=True)


if __name__ == "__main__":
    main()
