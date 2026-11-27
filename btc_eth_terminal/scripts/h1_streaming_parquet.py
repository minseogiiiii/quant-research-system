"""Bounded archive-to-Parquet, independent replay parity, atomic candidate publication.

Reuses the existing large durable ingress (80GB maximum) and q293 sealed replay.
No legacy limit changes. Raw data stays local, source pin is required externally.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from f5_independent_clock import milliseconds
from h1_historical_contract import need, normalized
from independent_soak_replay import strict_json
from q293_epoch_audit import Epoch, identity, scan
from v36_3_durable_archive_gate import audit

PACKET = pa.schema(
    [(k, pa.string()) for k in ("source_manifest_sha256", "epoch", "venue")]
    + [
        ("connection_id", pa.int64()),
        ("sequence", pa.int64()),
        ("receiver_ms", pa.int64()),
        ("receiver_utc", pa.string()),
        ("receiver_monotonic_ns", pa.int64()),
        ("channel", pa.string()),
        ("chunk", pa.string()),
        ("payload_sha256", pa.string()),
        ("payload", pa.string()),
    ]
)
PREFIX = [
    ("sequence", pa.int64()),
    ("event_index", pa.int64()),
    ("item_index", pa.int64()),
    ("receiver_ms", pa.int64()),
    ("product", pa.string()),
    ("kind", pa.string()),
]
LEVEL = pa.schema(
    PREFIX
    + [
        ("side", pa.string()),
        ("price", pa.string()),
        ("quantity", pa.string()),
        ("venue_event_utc", pa.string()),
    ]
)
TRADE = pa.schema(
    PREFIX
    + [
        ("maker_side", pa.string()),
        ("trade_id", pa.string()),
        ("price", pa.string()),
        ("quantity", pa.string()),
        ("venue_event_utc", pa.string()),
    ]
)
SCHEMAS = {"packets": PACKET, "l2": LEVEL, "trades": TRADE}
MAX_LINE = 18000000
MAX_BATCH_BYTES = 16000000
MAX_ROWS = 10000


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1048576), b""):
            h.update(b)
    return h.hexdigest()


def feed_hash(h, row):
    h.update(
        (
            json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n"
        ).encode()
    )


def rows(root, m, pin):
    prior = -1
    last = None
    mono = -1
    for c in m["chunks"]:
        path = root / c["file"]
        before = identity(path)
        h = hashlib.sha256()
        count = 0
        with path.open("rb") as f:
            while line := f.readline(MAX_LINE + 1):
                need(len(line) <= MAX_LINE and line.endswith(b"\n"), "LINE")
                h.update(line)
                count += 1
                row = strict_json(line)
                wire = strict_json(row["raw_text"])
                p, levels, t = normalized(row, wire, pin, c["file"])
                need(
                    p["sequence"] == prior + 1
                    and (last is None or p["receiver_ms"] >= last)
                    and p["receiver_monotonic_ns"] >= mono,
                    "CLOCK_SEQUENCE",
                )
                prior = p["sequence"]
                last = p["receiver_ms"]
                mono = p["receiver_monotonic_ns"]
                yield p, levels, t
        need(
            identity(path) == before
            and h.hexdigest() == c["archive_sha256"]
            and count == c["messages"],
            "CHUNK_CHANGED",
        )
    need(prior + 1 == m["messages"], "COUNT")


def verify_tables(folder, expected, witness, *, timings=None):
    db = duckdb.connect(
        ":memory:",
        config={
            "memory_limit": "256MB",
            "threads": "1",
            "preserve_insertion_order": "true",
            "temp_directory": str(folder / "duckdb-temp"),
        },
    )
    try:
        receipts = {}
        epoch = Epoch(0)
        for name, schema in SCHEMAS.items():
            path = folder / (name + ".parquet")
            started = time.monotonic()
            # The converter writes ordered row groups. Avoid sorting the entire
            # archive again; explicit monotonic keys AND canonical hash fail closed
            # if the scan order or physical file order ever changes.
            cursor = db.execute("SELECT * FROM read_parquet(?)", [str(path)])
            h = hashlib.sha256()
            count = 0
            prior = None
            while batch := cursor.fetchmany(256):
                for values in batch:
                    r = dict(zip(schema.names, values, strict=True))
                    key = (
                        (r["sequence"],)
                        if name == "packets"
                        else (r["sequence"], r["event_index"], r["item_index"])
                    )
                    need(
                        all(type(k) is int and k >= 0 for k in key)
                        and (prior is None or key > prior),
                        "PARQUET_PHYSICAL_ORDER",
                    )
                    prior = key
                    feed_hash(h, r)
                    count += 1
                    if name == "packets":
                        need(
                            milliseconds(r["receiver_utc"]) == r["receiver_ms"],
                            "PARQUET_CLOCK",
                        )
                        epoch.consume(
                            dict(
                                connection_id=r["connection_id"],
                                received_utc=r["receiver_utc"],
                                local_monotonic_ns=str(r["receiver_monotonic_ns"]),
                                raw_text=r["payload"],
                                sha256=r["payload_sha256"],
                            ),
                            None,
                            "primary",
                        )
            need(
                (count, h.hexdigest()) == expected[name],
                "PARQUET_" + name.upper() + "_PARITY",
            )
            receipts[name] = {
                "rows": count,
                "canonical_rows_sha256": h.hexdigest(),
                "file_sha256": digest(path),
            }
            if timings is not None:
                timings["verify_" + name + "_seconds"] = time.monotonic() - started
        result = epoch.summary()
        need(
            result["book_sha256"] == witness["epochs"][0]["book_sha256"]
            and result["messages"] == witness["sealed_messages"],
            "PARQUET_REPLAY",
        )
        return receipts, result
    finally:
        db.close()


def convert(root, pin, destination, *, fixture=False, timings=None):
    root = Path(root)
    dest = Path(destination)
    need(not dest.exists() and not dest.is_symlink(), "OUTPUT_EXISTS")
    started = time.monotonic()
    cert = audit(root, pin)
    if timings is not None:
        timings["durable_admission_seconds"] = time.monotonic() - started
    raw = (root / "manifest.json").read_bytes()
    m = strict_json(raw)
    need(hashlib.sha256(raw).hexdigest() == pin, "PIN")
    if fixture:
        need(
            m.get("synthetic_fixture") is True
            and not cert["eligible_for_archived_replay"]
            and not cert["partial_chunks_not_admitted"]
            and not cert["manifest_tmp_not_admitted"],
            "FIXTURE_ONLY",
        )
    else:
        need(cert["eligible_for_archived_replay"], "NOT_CLEAN")
    started = time.monotonic()
    witness = scan(root, pin, "primary")
    if timings is not None:
        timings["raw_epoch_replay_seconds"] = time.monotonic() - started
    need(
        len(witness["epochs"]) == 1
        and not witness["unsealed_entries_excluded"]
        and not witness["manifest_tmp_excluded"]
        and witness["sealed_messages"] == m["messages"],
        "SEALED_EPOCH",
    )
    with tempfile.TemporaryDirectory(prefix=".h1-stage-", dir=dest.parent) as tmp:
        staging = Path(tmp) / "dataset"
        staging.mkdir()
        started = time.monotonic()
        writers = {
            name: pq.ParquetWriter(
                staging / (name + ".parquet"), schema, compression="zstd"
            )
            for name, schema in SCHEMAS.items()
        }
        buffers = {name: [] for name in SCHEMAS}
        sizes = dict.fromkeys(SCHEMAS, 0)
        counts = dict.fromkeys(SCHEMAS, 0)
        hashes = {name: hashlib.sha256() for name in SCHEMAS}

        def flush(name):
            if buffers[name]:
                writers[name].write_table(
                    pa.Table.from_pylist(buffers[name], schema=SCHEMAS[name])
                )
                buffers[name].clear()
                sizes[name] = 0

        try:
            for p, levels, t in rows(root, m, pin):
                for name, items in (("packets", [p]), ("l2", levels), ("trades", t)):
                    for item in items:
                        size = sum(
                            len(v.encode()) if isinstance(v, str) else 8
                            for v in item.values()
                        )
                        if (
                            sizes[name] + size > MAX_BATCH_BYTES
                            or len(buffers[name]) >= MAX_ROWS
                        ):
                            flush(name)
                        feed_hash(hashes[name], item)
                        counts[name] += 1
                        buffers[name].append(item)
                        sizes[name] += size
            for name in SCHEMAS:
                flush(name)
        finally:
            for w in writers.values():
                w.close()
        need((root / "manifest.json").read_bytes() == raw, "MANIFEST_CHANGED")
        expected = {name: (counts[name], hashes[name].hexdigest()) for name in SCHEMAS}
        if timings is not None:
            timings["normalization_and_write_seconds"] = time.monotonic() - started
        receipt, final = verify_tables(staging, expected, witness, timings=timings)
        need(not dest.exists(), "OUTPUT_RACE")
        report = {
            "schema": "h1_historical_dataset_v1",
            "source_manifest_sha256": pin,
            "epoch": pin + ":0",
            "source_kind": "SYNTHETIC_TEST_ONLY" if fixture else "ARCHIVE_REPLAY",
            "source_messages": m["messages"],
            "tables": receipt,
            "book_sha256": final["book_sha256"],
            "independent_replay_parity": True,
            "research_eligible": not fixture,
            "live_feed": False,
            "oos_approved": False,
            "execution_authorized": False,
            "alpha_confirmed": False,
        }
        (staging / "dataset.json").write_text(json.dumps(report, sort_keys=True) + "\n")
        os.rename(staging, dest)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("archive", type=Path)
    p.add_argument("--manifest-sha256", required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(convert(a.archive, a.manifest_sha256, a.output), sort_keys=True))


if __name__ == "__main__":
    main()
