"""Q29.3-B2/B3: bounded sealed-prefix per-epoch audit and trade-ID joins.

Forensic only. An unsealed tail is never replayed or promoted; an epoch
verified to its last sealed record is not proof of a complete capture.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from pathlib import Path

from independent_soak_replay import parse_utc, price, strict_json
from v23_causal_microstructure import Book

PRODUCTS = ("BTC-USD", "ETH-USD")
MAX_LINE = 18_000_000
MAX_CHUNK = 32_000_000
MAX_CHUNKS = 20_000
MAX_MESSAGES = 30_000_000
MAX_LEVELS = 250_000
MAX_TRADES = 5_000_000


def need(ok, label):
    if not ok:
        raise ValueError(label)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def identity(path):
    s = path.lstat()
    need(stat.S_ISREG(s.st_mode) and s.st_nlink == 1, "EPOCH_FILE_UNSAFE")
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def digest_book(book):
    rows = [(side, format(px.normalize(), "f"), format(q.normalize(), "f"))
            for side in ("bid", "offer") for px, q in sorted(book[side].items())]
    return sha(json.dumps(rows, separators=(",", ":")).encode())


class TradeIndex:
    """Disk-backed exact product/trade ID join, never an L2 equivalence claim."""

    def __init__(self, path):
        path = Path(path)
        need(not path.exists() and not path.is_symlink(), "TRADE_INDEX_EXISTS")
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.execute("PRAGMA cache_size=-8192")
        self.db.execute("PRAGMA max_page_count=262144")
        self.db.execute(
            "CREATE TABLE trades (source TEXT, product TEXT, id TEXT, sig TEXT,"
            " PRIMARY KEY(source,product,id)) WITHOUT ROWID")
        self.count = 0

    def add(self, source, tr):
        p, tid = tr["product_id"], tr["trade_id"]
        need(source in ("primary", "secondary") and p in PRODUCTS
             and type(tid) is str and 0 < len(tid) <= 128
             and tr["side"] in ("BUY", "SELL"), "TRADE_ID_SCHEMA")
        fields = [format(price(tr["price"]).normalize(), "f"),
                  format(price(tr["size"]).normalize(), "f"),
                  parse_utc(tr["time"]).isoformat(), tr["side"]]
        signature = sha(json.dumps(fields, separators=(",", ":")).encode())
        key = (source, p, sha(tid.encode()))
        prior = self.db.execute(
            "SELECT sig FROM trades WHERE source=? AND product=? AND id=?", key
        ).fetchone()
        if prior:
            need(prior[0] == signature, "TRADE_ID_CONFLICT_WITHIN_SOURCE")
            return
        need(self.count < MAX_TRADES, "TRADE_INDEX_LIMIT")
        self.db.execute("INSERT INTO trades VALUES (?,?,?,?)", (*key, signature))
        self.count += 1

    def compare(self):
        self.db.commit()
        result = {}
        for p in PRODUCTS:
            counts = dict(self.db.execute(
                "SELECT source,count(*) FROM trades WHERE product=? GROUP BY source",
                (p,)))
            matched, divergent = self.db.execute(
                "SELECT count(*),coalesce(sum(a.sig!=b.sig),0) FROM trades a "
                "JOIN trades b ON a.product=b.product AND a.id=b.id "
                "WHERE a.source='primary' AND b.source='secondary' AND a.product=?",
                (p,)).fetchone()
            result[p] = {"primary_ids": counts.get("primary", 0),
                         "secondary_ids": counts.get("secondary", 0),
                         "common_ids": matched, "matching_ids": matched - divergent,
                         "divergent_ids": divergent,
                         "l2_equivalence_certified": False}
        return result

    def close(self):
        self.db.close()


class Epoch:
    def __init__(self, cid):
        self.cid = cid
        self.seq = -1
        self.books = {p: Book() for p in PRODUCTS}
        self.shadow = {p: {"bid": {}, "offer": {}} for p in PRODUCTS}
        self.first_ns = self.last_ns = None
        self.heartbeats = 0
        self.updates = dict.fromkeys(PRODUCTS, 0)

    def consume(self, packet, trades, source):
        need(packet["connection_id"] == self.cid, "EPOCH_CONNECTION")
        raw = packet["raw_text"].encode()
        need(sha(raw) == packet["sha256"], "EPOCH_MESSAGE_DIGEST")
        wire = strict_json(raw)
        seq = wire["sequence_num"]
        need(type(seq) is int and seq == self.seq + 1, "EPOCH_SEQUENCE_GAP")
        mono = packet["local_monotonic_ns"]
        need(type(mono) is str and mono.isascii() and mono.isdigit()
             and len(mono) <= 32, "EPOCH_CLOCK")
        mono = int(mono)
        need(self.last_ns is None or mono >= self.last_ns, "EPOCH_CLOCK")
        if self.first_ns is None:
            self.first_ns = mono
        self.last_ns = mono
        parse_utc(packet["received_utc"])
        parse_utc(wire["timestamp"])
        self.seq = seq
        channel = wire["channel"]
        if channel == "l2_data":
            for ev in wire["events"]:
                p, kind = ev["product_id"], ev["type"]
                need(p in PRODUCTS, "EPOCH_PRODUCT")
                main, shadow = self.books[p], self.shadow[p]
                main.apply(kind, ev["updates"])
                touched = set()
                for level in ev["updates"]:
                    side = level["side"]
                    need(side in shadow, "EPOCH_SIDE")
                    px = price(level["price_level"])
                    qty = price(level["new_quantity"], zero=True)
                    parse_utc(level["event_time"])
                    touched.add((side, px))
                    if qty:
                        shadow[side][px] = qty
                    else:
                        shadow[side].pop(px, None)
                for side in ("bid", "offer"):
                    levels = main.level[side]
                    need(len(levels) == len(shadow[side]) <= MAX_LEVELS,
                         "EPOCH_DEPTH_DIVERGENCE")
                    if len(main.heap[side]) > 3 * len(levels) + 1024:
                        import heapq

                        main.heap[side] = [
                            -px if side == "bid" else px for px in levels]
                        heapq.heapify(main.heap[side])
                need(all(main.level[s].get(px) == shadow[s].get(px)
                         for s, px in touched), "EPOCH_LEVEL_DIVERGENCE")
                main.best()
                if kind == "update":
                    self.updates[p] += 1
        elif channel == "heartbeats":
            self.heartbeats += 1
        elif channel == "market_trades":
            for ev in wire["events"]:
                need(ev["type"] in ("snapshot", "update"), "EPOCH_TRADE_TYPE")
                for tr in ev["trades"]:
                    if trades is not None:
                        trades.add(source, tr)
        else:
            need(channel == "subscriptions", "EPOCH_CHANNEL")
        return len(raw)

    def summary(self):
        for p in PRODUCTS:
            need(self.books[p].level == self.shadow[p], "EPOCH_FINAL_DIVERGENCE")
        ready = all(self.books[p].initialized and self.books[p].best() is not None
                    and self.updates[p] > 0 for p in PRODUCTS)
        return {"connection": self.cid, "messages": self.seq + 1,
                "first_monotonic_ns": self.first_ns,
                "last_monotonic_ns": self.last_ns,
                "both_books_initialized_and_updated": ready,
                "heartbeat_messages": self.heartbeats,
                "book_sha256": {p: digest_book(self.shadow[p]) for p in PRODUCTS},
                "sealed_prefix_replayed": True, "complete_epoch_certified": False}


def scan(root, pin, source, trades=None):
    """Validate only manifest-sealed chunks. Any discrepancy rejects the audit."""
    root = Path(root)
    need(stat.S_ISDIR(root.lstat().st_mode), "EPOCH_ROOT")
    manifest = root / "manifest.json"
    before = identity(manifest)
    need(0 < before[2] <= 24_000_000, "EPOCH_MANIFEST_SIZE")
    raw = manifest.read_bytes()
    need(sha(raw) == pin, "EPOCH_MANIFEST_PIN")
    m = strict_json(raw)
    need(m["format"] == "btc_eth_raw_soak_v10"
         and m["source"] == "wss://advanced-trade-ws.coinbase.com"
         and m["products"] == list(PRODUCTS) and m["orders_enabled"] is False
         and m["executable_alpha_verified"] is False, "EPOCH_PROVENANCE")
    entries = m["chunks"]
    need(type(entries) is list and len(entries) <= MAX_CHUNKS, "EPOCH_CHUNK_LIMIT")
    folder = root / "chunks"
    need(stat.S_ISDIR(folder.lstat().st_mode), "EPOCH_CHUNK_DIRECTORY")
    actual = {p.name for p in folder.iterdir()}
    listed = set()
    epochs = []
    epoch = None
    messages = total_bytes = 0
    for index, item in enumerate(entries):
        cid = item["connection_id"]
        need(type(cid) is int and 0 <= cid < 1000, "EPOCH_CONNECTION")
        name = f"chunk_{index:05d}_conn_{cid:03d}.jsonl"
        need(item["file"] == "chunks/" + name and name in actual,
             "EPOCH_CHUNK_NAME")
        listed.add(name)
        path = folder / name
        st = identity(path)
        need(0 < st[2] <= MAX_CHUNK, "EPOCH_CHUNK_SIZE")
        if epoch is None or epoch.cid != cid:
            if epoch is not None:
                epochs.append(epoch.summary())
                need(cid > epoch.cid, "EPOCH_CONNECTION_ORDER")
            epoch = Epoch(cid)
        first = epoch.seq + 1
        h = hashlib.sha256()
        rows = raw_bytes = 0
        first_utc = last_utc = None
        with path.open("rb") as stream:
            while line := stream.readline(MAX_LINE + 1):
                need(len(line) <= MAX_LINE and line.endswith(b"\n"), "EPOCH_LINE")
                h.update(line)
                packet = strict_json(line)
                raw_bytes += epoch.consume(packet, trades, source)
                if first_utc is None:
                    first_utc = packet["received_utc"]
                last_utc = packet["received_utc"]
                rows += 1
                messages += 1
                need(messages <= MAX_MESSAGES, "EPOCH_MESSAGE_LIMIT")
        need(identity(path) == st and h.hexdigest() == item["archive_sha256"]
             and rows == item["messages"] and raw_bytes == item["raw_text_bytes"]
             and st[2] == item["archived_line_bytes"]
             and first == item["first_sequence"] and epoch.seq == item["last_sequence"]
             and first_utc == item["first_received_utc"]
             and last_utc == item["last_received_utc"], "EPOCH_CHUNK_MISMATCH")
        total_bytes += st[2]
        if trades is not None:
            trades.db.commit()
    if epoch is not None:
        epochs.append(epoch.summary())
    need(identity(manifest) == before and sha(manifest.read_bytes()) == pin,
         "EPOCH_MANIFEST_CHANGED")
    need({p.name for p in folder.iterdir()} == actual, "EPOCH_DIRECTORY_CHANGED")
    unsealed = actual - listed
    need(all(re.fullmatch(r"chunk_\d{5}_conn_\d{3}\.jsonl(?:\.partial)?", p)
             for p in unsealed), "EPOCH_UNKNOWN_UNSEALED_ENTRY")
    return {"schema": "q293_sealed_epoch_forensics_v1", "manifest_sha256": pin,
            "sealed_chunks": len(entries), "sealed_messages": messages,
            "sealed_bytes": total_bytes, "epochs": epochs,
            "unsealed_entries_excluded": len(unsealed),
            "manifest_tmp_excluded": (root / "manifest.json.tmp").exists(),
            "zero_gap_certified": False, "historical_gap_recovered": False,
            "real_orders_enabled": False}
