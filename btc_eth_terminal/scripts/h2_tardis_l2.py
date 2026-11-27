"""Local-only Tardis Coinbase Exchange L2 CSV adapter. No native sequence claim."""

from __future__ import annotations
import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import tempfile
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path
import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from f4_depth_buckets import units, decimal, SCALE
from post_quote_markout import IndexedBook
from h1_streaming_parquet import digest, feed_hash

HEADER = (
    "exchange",
    "symbol",
    "timestamp",
    "local_timestamp",
    "is_snapshot",
    "side",
    "price",
    "amount",
)
SCHEMA = pa.schema(
    [
        ("row_number", pa.int64()),
        ("epoch", pa.int32()),
        ("group_number", pa.int64()),
        ("venue_timestamp_us", pa.int64()),
        ("vendor_receiver_us", pa.int64()),
        ("is_snapshot", pa.bool_()),
        ("side", pa.string()),
        ("price", pa.string()),
        ("quantity", pa.string()),
    ]
)
MAX_ROWS = 100_000_000
MAX_BYTES = 80_000_000_000
MAX_LEVELS = 200_000
MAX_GROUP = 250_000
MAX_EPOCHS = 10_000
BATCH = 16_384


def need(ok, reason):
    if not ok:
        raise ValueError("H2_" + reason)


def clock(value):
    need(isinstance(value, str) and re.fullmatch(r"[0-9]{1,18}", value), "CLOCK")
    n = int(value)
    need(0 < n < 2**63, "CLOCK_RANGE")
    return n


def day_bounds(day):
    d = date.fromisoformat(day)
    need(d.isoformat() == day, "DATE")
    start = (d - date(1970, 1, 1)).days * 86400 * 10**6
    return start, start + 86400 * 10**6


def book_hash(book):
    h = hashlib.sha256()
    for side, levels in (("bid", book.bid), ("ask", book.offer)):
        for p, q in sorted(levels.items()):
            feed_hash(h, [side, decimal(p), decimal(q)])
    return h.hexdigest()


def convert(source, pin, out, *, symbol, day, fixture=False):
    source, out = Path(source), Path(out)
    need(
        symbol in ("BTC-USD", "ETH-USD") and re.fullmatch(r"[0-9a-f]{64}", pin),
        "IDENTITY",
    )
    need(
        source.is_file()
        and not source.is_symlink()
        and source.stat().st_size <= MAX_BYTES
        and not out.exists()
        and not out.is_symlink(),
        "PATH",
    )
    need(digest(source) == pin, "SOURCE_PIN")
    start, end = day_bounds(day)
    book = IndexedBook()
    epoch = -1
    group = -1
    prior = None
    previous_kind = None
    seen_rows = skipped = kept = byte_count = 0
    epochs = []
    group_count = 0
    epoch_start = None
    last_observed = None
    raw_hash = hashlib.sha256()
    canonical = hashlib.sha256()
    buf = []

    def finish_group():
        if group < 0:
            return
        b, a = book.best("bid"), book.best("offer")
        need(b is not None and a is not None and b[0] < a[0], "CROSSED_OR_EMPTY_BOOK")

    def finish_epoch():
        if epoch >= 0:
            finish_group()
            epochs.append(
                {
                    "epoch": epoch,
                    "first_vendor_receiver_us": epoch_start,
                    "last_vendor_receiver_us": last_observed,
                    "book_sha256": book_hash(book),
                }
            )

    with tempfile.TemporaryDirectory(prefix=".h2-", dir=out.parent) as tmp:
        dest = Path(tmp) / "dataset"
        dest.mkdir()
        path = dest / "vendor_l2.parquet"
        with (
            pq.ParquetWriter(path, SCHEMA, compression="zstd") as writer,
            gzip.open(source, "rb") as f,
        ):
            header = f.readline(4097)
            need(header == (",".join(HEADER) + "\n").encode(), "HEADER")
            byte_count = len(header)
            raw_hash.update(header)
            while line := f.readline(4097):
                need(len(line) <= 4096 and line.endswith(b"\n"), "LINE")
                byte_count += len(line)
                seen_rows += 1
                raw_hash.update(line)
                need(seen_rows <= MAX_ROWS and byte_count <= MAX_BYTES, "BUDGET")
                cols = next(csv.reader([line.decode("ascii")], strict=True))
                need(
                    len(cols) == 8 and cols[0] == "coinbase" and cols[1] == symbol,
                    "ROW_IDENTITY",
                )
                _, _, venue, recv, kind, side, price, amount = cols
                v, t = clock(venue), clock(recv)
                need(
                    start <= t < end and (prior is None or t >= prior), "RECEIVER_ORDER"
                )
                need(kind in ("true", "false") and side in ("bid", "ask"), "ROW_ENUM")
                p, q = units(price), units(amount)
                need(p > 0, "PRICE")
                snap = kind == "true"
                if epoch < 0 and not snap:
                    skipped += 1
                    prior = t
                    continue
                new_epoch = snap and previous_kind is not True
                if new_epoch:
                    finish_epoch()
                    epoch += 1
                    need(epoch < MAX_EPOCHS, "EPOCH_BUDGET")
                    book = IndexedBook()
                    epoch_start = t
                elif snap:
                    # strict profile: entire initial snapshot has one arrival clock
                    need(t == epoch_start, "MULTI_TIMESTAMP_SNAPSHOT_UNSUPPORTED")
                if new_epoch or t != prior or snap != previous_kind:
                    if not new_epoch:
                        finish_group()
                    group += 1
                    group_count = 0
                group_count += 1
                need(group_count <= MAX_GROUP, "GROUP_BUDGET")
                book.change("bid" if side == "bid" else "offer", p, q)
                need(len(book.bid) + len(book.offer) <= MAX_LEVELS, "LEVEL_BUDGET")
                row = dict(
                    row_number=seen_rows,
                    epoch=epoch,
                    group_number=group,
                    venue_timestamp_us=v,
                    vendor_receiver_us=t,
                    is_snapshot=snap,
                    side=side,
                    price=price,
                    quantity=amount,
                )
                feed_hash(canonical, row)
                buf.append(row)
                kept += 1
                if len(buf) >= BATCH:
                    writer.write_table(pa.Table.from_pylist(buf, schema=SCHEMA))
                    buf.clear()
                prior = t
                previous_kind = snap
                last_observed = t
            finish_epoch()
            need(epoch >= 0 and kept > 0, "NO_SNAPSHOT")
            if buf:
                writer.write_table(pa.Table.from_pylist(buf, schema=SCHEMA))
        need(digest(source) == pin, "SOURCE_CHANGED")
        # Independent Decimal reconstruction from Parquet. No reused IndexedBook
        # or units parser; canonical hash binds row order and exact original text.
        db = duckdb.connect(
            ":memory:",
            config={
                "memory_limit": "256MB",
                "threads": "2",
                "temp_directory": str(Path(tmp) / "spill"),
            },
        )
        h = hashlib.sha256()
        count = 0
        current = -1
        first = last = None
        state = {}
        witness = []
        last_row = skipped

        def witness_end():
            if current < 0:
                return
            bids = [p for side, p in state if side == "bid"]
            asks = [p for side, p in state if side == "ask"]
            need(bids and asks and max(bids) < min(asks), "WITNESS_BOOK")
            hh = hashlib.sha256()

            def canon(x):
                v = format(x, "f")
                return v.rstrip("0").rstrip(".") if "." in v else v

            for (side, p), q in sorted(
                state.items(), key=lambda kv: (0 if kv[0][0] == "bid" else 1, kv[0][1])
            ):
                feed_hash(hh, [side, canon(p), canon(q)])
            witness.append(
                {
                    "epoch": current,
                    "first_vendor_receiver_us": first,
                    "last_vendor_receiver_us": last,
                    "book_sha256": hh.hexdigest(),
                }
            )

        try:
            cursor = db.execute(
                "SELECT * FROM read_parquet(?) ORDER BY row_number", [str(path)]
            )
            while batch := cursor.fetchmany(4096):
                for vals in batch:
                    row = dict(zip(SCHEMA.names, vals, strict=True))
                    feed_hash(h, row)
                    count += 1
                    need(row["row_number"] == last_row + 1, "WITNESS_ROW")
                    last_row = row["row_number"]
                    if row["epoch"] != current:
                        witness_end()
                        need(
                            row["epoch"] == current + 1 and row["is_snapshot"],
                            "WITNESS_EPOCH",
                        )
                        current = row["epoch"]
                        first = row["vendor_receiver_us"]
                        state = {}
                    last = row["vendor_receiver_us"]
                    key = (row["side"], Decimal(row["price"]))
                    q = Decimal(row["quantity"])
                    if q:
                        state[key] = q
                    else:
                        state.pop(key, None)
            witness_end()
        finally:
            db.close()
        need(
            count == kept
            and h.hexdigest() == canonical.hexdigest()
            and witness == epochs,
            "INDEPENDENT_PARITY",
        )
        report = {
            "schema": "h2_vendor_l2_v1",
            "source_kind": "SYNTHETIC_TEST_ONLY" if fixture else "VENDOR_NORMALIZED_L2",
            "source_sha256": pin,
            "decompressed_sha256": raw_hash.hexdigest(),
            "source_bytes": source.stat().st_size,
            "decompressed_bytes": byte_count,
            "exchange": "coinbase",
            "symbol": symbol,
            "utc_day": day,
            "source_rows": seen_rows,
            "pre_snapshot_rows_skipped": skipped,
            "admitted_rows": kept,
            "epochs": epochs,
            "groups": group + 1,
            "canonical_rows_sha256": canonical.hexdigest(),
            "parquet_sha256": digest(path),
            "independent_final_epoch_book_parity": True,
            "receiver_clock": "VENDOR_CAPTURE_US",
            "native_sequence_available": False,
            "native_monotonic_available": False,
            "zero_gap_certified": False,
            "cross_epoch_continuity": False,
            "local_only": True,
            "research_eligible": False,
            "oos_approved": False,
            "alpha_confirmed": False,
            "execution_authorized": False,
        }
        (dest / "dataset.json").write_text(
            json.dumps(report, sort_keys=True, allow_nan=False) + "\n"
        )
        need(not out.exists(), "OUTPUT_RACE")
        os.rename(dest, out)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("source", type=Path)
    p.add_argument("--sha256", required=True)
    p.add_argument("--symbol", choices=["BTC-USD", "ETH-USD"], required=True)
    p.add_argument("--day", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--confirm-local-authorized-use", action="store_true", required=True)
    a = p.parse_args()
    result = convert(a.source, a.sha256, a.output, symbol=a.symbol, day=a.day)
    # Do not print vendor-derived rows, price data or book fingerprints.
    print(
        json.dumps(
            {
                "status": "LOCAL_CONVERSION_PASS",
                "admitted_rows": result["admitted_rows"],
                "epochs": len(result["epochs"]),
                "research_eligible": False,
            }
        )
    )


if __name__ == "__main__":
    main()
