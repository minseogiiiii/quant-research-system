"""Fail-closed archival verifier for v10 Coinbase BTC/ETH public L2 soak captures.

Validates independently of the Node writer. Raw exchange output is treated as
untrusted. A valid *archive* does not authenticate the exchange or prove an
alpha, fill, clock synchronisation or true historical availability timestamp.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import re
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

PRODUCTS = ("BTC-USD", "ETH-USD")
CHANNELS = frozenset(("l2_data", "market_trades", "heartbeats", "subscriptions"))
SAFE_CHUNK = re.compile(r"chunk_\d{5}_conn_\d{3}\.jsonl\Z")
MAX_LINE = 18_500_000
MAX_CHUNK = 18_500_000
MAX_BYTES = 1_250_000_000


class SoakAuditError(ValueError):
    """Source recording cannot pass the specified admission gate."""


def _unique(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise SoakAuditError("Duplicate JSON key: " + str(k))
        result[k] = v
    return result


def _constant(v):
    raise SoakAuditError("Nonfinite JSON token: " + v)


def _json(data):
    try:
        return json.loads(data, object_pairs_hook=_unique, parse_constant=_constant)
    except (ValueError, TypeError, UnicodeError) as e:
        raise SoakAuditError("Malformed or ambiguous JSON") from e


def _utc(value):
    if not isinstance(value, str):
        raise SoakAuditError("Timestamp is not a string")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SoakAuditError("Invalid timestamp") from exc
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise SoakAuditError("Timestamp not UTC")
    return dt


def _positive(value, allow_zero=False):
    if not isinstance(value, str) or not value.strip():
        raise SoakAuditError("Price/quantity must be decimal text")
    try:
        d = Decimal(value)
    except InvalidOperation as exc:
        raise SoakAuditError("Invalid price/quantity") from exc
    if not d.is_finite() or d < 0 or (d == 0 and not allow_zero):
        raise SoakAuditError("Nonfinite/negative/zero price or quantity")
    return d


def _file(path, base, max_bytes):
    if (
        path.is_symlink()
        or not path.is_file()
        or not path.resolve().is_relative_to(base.resolve())
    ):
        raise SoakAuditError("Missing/symlinked/escaped evidence file")
    n = path.stat().st_size
    if n <= 0 or n > max_bytes:
        raise SoakAuditError("Evidence file size outside bounds")
    return n


def audit(directory: Path) -> dict:
    source_path = Path(directory)
    if source_path.is_symlink():
        raise SoakAuditError("Symlink archive root")
    root = source_path.resolve()
    if not root.is_dir():
        raise SoakAuditError("Invalid archive root")
    _file(
        root / "manifest.json", root, 5_000_000
    )  # v20 long-run chunk ledgers may exceed legacy 500KB
    manifest = _json((root / "manifest.json").read_bytes())
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != "btc_eth_raw_soak_v10"
    ):
        raise SoakAuditError("Unsupported archive schema")
    if (
        manifest.get("source") != "wss://advanced-trade-ws.coinbase.com"
        or manifest.get("products") != list(PRODUCTS)
        or manifest.get("subscribed_channels")
        != ["level2", "market_trades", "heartbeats"]
        or manifest.get("orders_enabled") is not False
        or manifest.get("strategy_execution_enabled") is not False
        or manifest.get("point_in_time_historical_availability_verified") is not False
        or manifest.get("executable_alpha_verified") is not False
        or manifest.get("sequence_scope") != "PER_WEBSOCKET_CONNECTION"
    ):
        raise SoakAuditError("Source, instrument, or safety provenance contradiction")
    # v13 cap expansion is explicitly declared and bounded. Existing v10-v12
    # archived manifests retain their original 300MB maximum by policy.
    producer = manifest.get("producer_release")
    rolling = manifest.get("rolling_policy")
    if producer in ("v13_rolling_bounded", "v20_multiday_bounded"):
        if not isinstance(rolling, dict):
            raise SoakAuditError("Missing v13 rolling policy")
        expected_policy_keys = {
            "chunk_target_bytes",
            "raw_budget_bytes",
            "archived_jsonl_budget_bytes",
            "single_message_can_exceed_chunk_target",
            "source_connection_continuous_across_chunk_files",
        }
        if set(rolling) != expected_policy_keys:
            raise SoakAuditError("Unknown/incomplete v13 rolling policy fields")
        raw_cap = rolling["raw_budget_bytes"]
        chunk_target = rolling["chunk_target_bytes"]
        jsonl_cap = rolling["archived_jsonl_budget_bytes"]
        ceiling_raw = MAX_BYTES if producer == "v13_rolling_bounded" else 60 * 10**9
        ceiling_archive = (
            1600 * 1024 * 1024 if producer == "v13_rolling_bounded" else 80 * 10**9
        )
        if (
            type(raw_cap) is not int
            or not 1024 <= raw_cap <= ceiling_raw
            or type(chunk_target) is not int
            or not 1024 <= chunk_target <= 8_000_000
            or type(jsonl_cap) is not int
            or not 1024 <= jsonl_cap <= ceiling_archive
            or jsonl_cap < raw_cap
            or rolling["single_message_can_exceed_chunk_target"] is not True
            or rolling["source_connection_continuous_across_chunk_files"] is not True
            or manifest.get("max_capture_bytes") != raw_cap
        ):
            raise SoakAuditError("v13 rolling source limits contradict envelope")
    elif producer is None and rolling is None:
        raw_cap = 300_000_000
        jsonl_cap = 800 * 1024 * 1024
        chunk_target = 8_000_000
        if manifest.get("max_capture_bytes") not in (None, 300_000_000):
            raise SoakAuditError("Legacy source raw cap mismatch")
    else:
        raise SoakAuditError("Unsupported producer/rolling policy combination")
    # An opt-in disconnect/fault experiment is never point-in-time continuous
    # research evidence, even when a producer later reports fresh snapshots.
    controlled = manifest.get("controlled_fault_injection")
    if controlled is not None:
        if (
            not isinstance(controlled, dict)
            or set(controlled) != {"mode", "after_messages", "never_admit"}
            or controlled["mode"] != "PUBLIC_FEED_DISCONNECT"
            or type(controlled["after_messages"]) is not int
            or not 1 <= controlled["after_messages"] <= 1_000_000
            or controlled["never_admit"] is not True
        ):
            raise SoakAuditError("Malformed controlled-fault experiment declaration")
        if (
            manifest.get("ready_for_research") is not False
            or manifest.get("status")
            == "CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH"
        ):
            raise SoakAuditError(
                "Fault experiment cannot be promoted to continuous research"
            )
    # Test-fixture captures are always forensic-only, regardless of apparent
    # heartbeat/sequence completeness. Do not promote mock WebSocket evidence.
    synthetic = manifest.get("synthetic_fixture", False)
    if type(synthetic) is not bool:
        raise SoakAuditError("Ambiguous synthetic fixture provenance flag")
    if synthetic and (
        manifest.get("ready_for_research") is not False
        or manifest.get("status") == "CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH"
    ):
        raise SoakAuditError(
            "Synthetic recording falsely admitted as real-source evidence"
        )
    status = manifest.get("status")
    if status not in (
        "CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH",
        "CAPTURE_COMPLETED_WITH_QUARANTINED_GAPS",
        "CAPTURE_INCOMPLETE_OR_INVALID",
    ):
        raise SoakAuditError("Capture has not reached terminal state")
    start = _utc(manifest.get("sampled_from_utc"))
    end = _utc(manifest.get("finished_at_utc"))
    max_elapsed = 3800 if producer != "v20_multiday_bounded" else 259260
    max_seconds = 3600 if producer != "v20_multiday_bounded" else 259200
    if not start < end or (end - start).total_seconds() > max_elapsed:
        raise SoakAuditError("Invalid capture period")
    if (
        type(manifest.get("required_duration_seconds")) is not int
        or not 10 <= manifest["required_duration_seconds"] <= max_seconds
    ):
        raise SoakAuditError("Invalid duration policy")
    # A producer's COMPLETED claim must cover the declared wall-clock observation
    # interval. This does not prove continuous provider connectivity or NTP sync.
    if (
        status == "CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH"
        and (end - start).total_seconds() + 2 < manifest["required_duration_seconds"]
    ):
        raise SoakAuditError("Capture terminated before declared duration elapsed")
    chunks = manifest.get("chunks")
    connections = manifest.get("connections")
    faults = manifest.get("faults")
    if (
        not isinstance(chunks, list)
        or not isinstance(connections, list)
        or not isinstance(faults, list)
    ):
        raise SoakAuditError("Missing chunk/connection/fault ledger")
    if (
        len(chunks) == 0
        or len(chunks) > 10000
        or len(connections) > 100
        or not connections
    ):
        raise SoakAuditError("No/oversized source session")
    if status == "CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH":
        # A rejected source packet or uncommitted write must never be hidden by
        # an otherwise plausible success manifest.
        if {p.name for p in root.iterdir()} != {"manifest.json", "chunks"}:
            raise SoakAuditError("Unexpected unlisted root archive evidence")
    conn_by_id = {}
    for i, c in enumerate(connections):
        if not isinstance(c, dict) or type(c.get("id")) is not int or c["id"] != i:
            raise SoakAuditError("Invalid connection enumeration")
        conn_by_id[i] = c
        connection_start = _utc(c.get("started_utc"))
        connection_end = _utc(c.get("ended_utc"))
        if (
            connection_start < start
            or connection_end > end + timedelta(seconds=2)
            or connection_end < connection_start
        ):
            raise SoakAuditError("Source connection outside manifest capture window")
    chunk_dir = root / "chunks"
    if chunk_dir.is_symlink() or not chunk_dir.is_dir():
        raise SoakAuditError("Missing safe chunk directory")
    actual = {p.name for p in chunk_dir.iterdir()}
    names = []
    for i, c in enumerate(chunks):
        if not isinstance(c, dict) or not isinstance(c.get("file"), str):
            raise SoakAuditError("Malformed chunk entry")
        file = c["file"]
        if (
            file
            != "chunks/" + f"chunk_{i:05d}_conn_{c.get('connection_id', -1):03d}.jsonl"
            or not SAFE_CHUNK.fullmatch(file.split("/")[-1])
        ):
            raise SoakAuditError("Noncanonical/order-inconsistent chunk filename")
        names.append(file.split("/")[-1])
    if len(names) != len(set(names)) or actual != set(names):
        raise SoakAuditError("Missing/unlisted chunk, partial chunk or unexpected file")
    if (root / "manifest.json.tmp").exists():
        raise SoakAuditError("Uncommitted manifest data")
    counters = {p: Counter() for p in PRODUCTS}
    books = {p: {"bid": {}, "offer": {}} for p in PRODUCTS}
    # Index ONLY: the canonical book remains the exact Decimal dictionary above.
    # Heap compaction is bounded; avoids O(depth) max/min on each tick.
    book_price_heaps = {p: {"bid": [], "offer": []} for p in PRODUCTS}

    def top_price(product, side):
        heap = book_price_heaps[product][side]
        levels = books[product][side]
        while heap:
            price = -heap[0] if side == "bid" else heap[0]
            if price in levels:
                return price
            heapq.heappop(heap)
        if levels:
            raise SoakAuditError("Book index lost a live price level")
        return None

    # Trade ID dedup is per connection. A fresh subscription snapshot can
    # legitimately replay trades from the previous, disconnected session.
    # Never aggregate these across connections into continuous market volume.
    seen_trade_ids = {i: {p: {} for p in PRODUCTS} for i in conn_by_id}
    last_seq = {}
    last_ns = {}
    last_wall = {}
    seen_snapshots = {i: set() for i in conn_by_id}
    first_sequence = {}
    conn_message_count = Counter()
    total_raw = 0
    total_messages = 0
    messages_before_ready = Counter()
    book_crossed = Counter()
    channel_counts = Counter()
    hb_per_conn = Counter()
    hb_first_receipt = {}
    hb_last_receipt = {}
    hb_last_counter = {}
    hb_max_gap_seconds = {}
    updates_per_conn = {i: Counter() for i in conn_by_id}
    for i, chunk in enumerate(chunks):
        file = root / chunk["file"]
        _file(file, root, MAX_CHUNK)
        c_id = chunk.get("connection_id")
        if type(c_id) is not int or c_id not in conn_by_id:
            raise SoakAuditError("Chunk with unknown connection id")
        digest = hashlib.sha256()
        row_count = 0
        raw_bytes = 0
        first = last = first_time = last_time = None
        with file.open("rb") as stream:
            for line in stream:
                if len(line) > MAX_LINE or not line.endswith(b"\n"):
                    raise SoakAuditError("Truncated/oversized source row")
                digest.update(line)
                record = _json(line)
                if not isinstance(record, dict) or record.get("connection_id") != c_id:
                    raise SoakAuditError(
                        "Connection identity differs from archived chunk"
                    )
                raw = record.get("raw_text")
                if not isinstance(raw, str):
                    raise SoakAuditError("Missing original WS text")
                payload = raw.encode("utf-8")
                raw_bytes += len(payload)
                if len(payload) > 9_000_000 or hashlib.sha256(
                    payload
                ).hexdigest() != record.get("sha256"):
                    raise SoakAuditError("Payload SHA/size invalid")
                m = _json(payload)
                if not isinstance(m, dict):
                    raise SoakAuditError("Non-object market event")
                seq = m.get("sequence_num")
                if type(seq) is not int or seq < 0 or seq > 2**53 - 1:
                    raise SoakAuditError("Invalid source sequence")
                if c_id not in last_seq:
                    if seq != 0:
                        raise SoakAuditError(
                            "New connection source sequence did not start at zero"
                        )
                    first_sequence[c_id] = seq
                elif seq != last_seq[c_id] + 1:
                    raise SoakAuditError("Silent sequence gap, duplicate, or replay")
                last_seq[c_id] = seq
                received = _utc(record.get("received_utc"))
                n = record.get("local_monotonic_ns")
                if not isinstance(n, str) or not n.isdecimal():
                    raise SoakAuditError("Invalid local monotonic timestamp")
                n = int(n)
                if c_id in last_ns and n <= last_ns[c_id]:
                    raise SoakAuditError("Non-monotonic local receipt time")
                if c_id in last_wall and received < last_wall[c_id]:
                    raise SoakAuditError("Local wall-clock order regression")
                last_ns[c_id] = n
                last_wall[c_id] = received
                if received < start - timedelta(
                    seconds=1
                ) or received > end + timedelta(seconds=1):
                    raise SoakAuditError("Message outside capture window")
                timestamp = _utc(m.get("timestamp"))
                if (timestamp - received).total_seconds() > 30 or (
                    received - timestamp
                ).total_seconds() > 120:
                    raise SoakAuditError(
                        "Feed envelope timestamp outside tolerated skew range"
                    )
                ch = m.get("channel")
                events = m.get("events")
                if ch not in CHANNELS or not isinstance(events, list):
                    raise SoakAuditError("Unexpected channel or events")
                channel_counts[ch] += 1
                if ch == "l2_data":
                    for event in events:
                        if (
                            not isinstance(event, dict)
                            or event.get("product_id") not in PRODUCTS
                        ):
                            raise SoakAuditError("Unknown book instrument")
                        p = event["product_id"]
                        kind = event.get("type")
                        if kind == "snapshot":
                            if p in seen_snapshots[c_id]:
                                raise SoakAuditError("Duplicate in-connection snapshot")
                            seen_snapshots[c_id].add(p)
                            counters[p]["snapshot"] += 1
                            books[p] = {"bid": {}, "offer": {}}
                            book_price_heaps[p] = {"bid": [], "offer": []}
                        elif kind == "update":
                            if p not in seen_snapshots[c_id]:
                                raise SoakAuditError(
                                    "Book update before snapshot on new connection"
                                )
                            counters[p]["update"] += 1
                            updates_per_conn[c_id][p] += 1
                        else:
                            raise SoakAuditError("Unknown book event type")
                        changes = event.get("updates")
                        if not isinstance(changes, list) or len(changes) > 500000:
                            raise SoakAuditError("Malformed book changes")
                        for change in changes:
                            if not isinstance(change, dict) or change.get(
                                "side"
                            ) not in ("bid", "offer"):
                                raise SoakAuditError("Unexpected book change side")
                            price = _positive(change.get("price_level"))
                            qty = _positive(change.get("new_quantity"), allow_zero=True)
                            _utc(change.get("event_time"))
                            which = change["side"]
                            side = books[p][which]
                            heap = book_price_heaps[p][which]
                            if qty == 0:
                                side.pop(price, None)
                            else:
                                is_new = price not in side
                                side[price] = qty
                                if is_new:
                                    heapq.heappush(
                                        heap, -price if which == "bid" else price
                                    )
                            if len(heap) > 3 * len(side) + 1024:
                                heap[:] = [(-v if which == "bid" else v) for v in side]
                                heapq.heapify(heap)
                        if (
                            books[p]["bid"]
                            and books[p]["offer"]
                            and top_price(p, "bid") >= top_price(p, "offer")
                        ):
                            book_crossed[p] += 1
                            raise SoakAuditError(
                                "Crossed/locked reconstructed orderbook"
                            )
                elif ch == "market_trades":
                    for event in events:
                        if (
                            not isinstance(event, dict)
                            or event.get("type") not in ("snapshot", "update")
                            or not isinstance(event.get("trades"), list)
                        ):
                            raise SoakAuditError("Malformed public trade event")
                        for trade in event["trades"]:
                            if (
                                not isinstance(trade, dict)
                                or trade.get("product_id") not in PRODUCTS
                            ):
                                raise SoakAuditError("Unexpected trade product")
                            p = trade["product_id"]
                            if trade.get("side") not in ("BUY", "SELL"):
                                raise SoakAuditError("Invalid trade side")
                            identifier = trade.get("trade_id")
                            if (
                                not isinstance(identifier, str)
                                or not identifier.strip()
                            ):
                                raise SoakAuditError("Invalid trade identifier")
                            price = _positive(trade.get("price"))
                            size = _positive(trade.get("size"))
                            exchange_time = _utc(trade.get("time"))
                            fingerprint = (
                                event["type"],
                                trade["side"],
                                price,
                                size,
                                exchange_time,
                                frozenset(trade.items()),
                            )
                            if identifier in seen_trade_ids[c_id][p]:
                                old_sequence, old_fingerprint = seen_trade_ids[c_id][p][
                                    identifier
                                ]
                                if (
                                    event["type"] != "update"
                                    or old_sequence == seq
                                    or old_fingerprint != fingerprint
                                ):
                                    raise SoakAuditError(
                                        "Conflicting/within-message trade ID replay"
                                    )
                                counters[p][
                                    "identical_update_trade_replays_excluded"
                                ] += 1
                                continue
                            seen_trade_ids[c_id][p][identifier] = (seq, fingerprint)
                            counters[p]["unique_trade_id"] += 1
                            if event["type"] == "snapshot":
                                counters[p]["historical_trade_snapshot"] += 1
                            else:
                                counters[p]["unique_update_trade"] += 1
                elif ch == "heartbeats":
                    hb_per_conn[c_id] += 1
                    for e in events:
                        if not isinstance(e, dict):
                            raise SoakAuditError("Malformed heartbeat")
                    if producer == "v20_multiday_bounded" and not synthetic:
                        if len(events) != 1:
                            raise SoakAuditError(
                                "Long-run source heartbeat cardinality invalid"
                            )
                        val = events[0].get("heartbeat_counter")
                        if type(val) is int:
                            counter = val
                        elif isinstance(val, str) and val.isdecimal():
                            counter = int(val)
                        else:
                            raise SoakAuditError(
                                "Long-run source heartbeat counter unavailable"
                            )
                        if counter < 0 or (
                            c_id in hb_last_counter
                            and counter != hb_last_counter[c_id] + 1
                        ):
                            raise SoakAuditError(
                                "Long-run heartbeat counter discontinuity"
                            )
                        hb_last_counter[c_id] = counter
                        if c_id in hb_last_receipt:
                            gap = (received - hb_last_receipt[c_id]).total_seconds()
                            if gap <= 0 or gap > 12:
                                raise SoakAuditError(
                                    "Long-run heartbeat receipt gap > 12s"
                                )
                            hb_max_gap_seconds[c_id] = max(
                                hb_max_gap_seconds.get(c_id, 0), gap
                            )
                        else:
                            hb_first_receipt[c_id] = received
                        hb_last_receipt[c_id] = received
                if len(seen_snapshots[c_id]) < 2:
                    messages_before_ready[c_id] += 1
                row_count += 1
                conn_message_count[c_id] += 1
                total_messages += 1
                first = seq if first is None else first
                last = seq
                first_time = received if first_time is None else first_time
                last_time = received
        if (
            producer in ("v13_rolling_bounded", "v20_multiday_bounded")
            and row_count > 1
            and file.stat().st_size > chunk_target
        ):
            raise SoakAuditError(
                "Rolling chunk expanded beyond bounded record boundary"
            )
        if (
            chunk.get("archive_sha256") != digest.hexdigest()
            or chunk.get("messages") != row_count
            or chunk.get("raw_text_bytes") != raw_bytes
            or chunk.get("archived_line_bytes") != file.stat().st_size
            or chunk.get("first_sequence") != first
            or chunk.get("last_sequence") != last
            or _utc(chunk.get("first_received_utc")) != first_time
            or _utc(chunk.get("last_received_utc")) != last_time
        ):
            raise SoakAuditError("Chunk metadata does not match actual archived data")
        total_raw += raw_bytes
        if total_raw > raw_cap:
            raise SoakAuditError("Archive exceeds bounded read size")
    for id, c in conn_by_id.items():
        if conn_message_count[id] != c.get("messages"):
            raise SoakAuditError("Session message tally mismatch")
        if c.get("snapshots") != {p: (p in seen_snapshots[id]) for p in PRODUCTS}:
            raise SoakAuditError("Source snapshot telemetry disagrees with raw")
        if c.get("became_ready") != (len(seen_snapshots[id]) == 2):
            raise SoakAuditError("Premature/invalid book readiness report")
        if c.get("heartbeat_count") != hb_per_conn[id] or c.get("updates") != {
            p: updates_per_conn[id][p] for p in PRODUCTS
        }:
            raise SoakAuditError("Source heartbeat/update telemetry disagrees with raw")
        if c.get("last_sequence") != last_seq.get(id):
            raise SoakAuditError("Source last sequence tally mismatch")
        if c.get("integrity_fault") not in (True, False):
            raise SoakAuditError("Missing boolean integrity fault status")
    if total_messages != manifest.get("messages") or total_raw != manifest.get("bytes"):
        raise SoakAuditError("Global manifest counts disagree with source")
    # v12 producer resource telemetry is informative rather than independent
    # attestation. Its archived-byte total must still reconcile to raw files.
    resource = manifest.get("resource_telemetry")
    if producer in ("v13_rolling_bounded", "v20_multiday_bounded") and resource is None:
        raise SoakAuditError("Missing v13 resource telemetry")
    if resource is not None:
        if not isinstance(resource, dict):
            raise SoakAuditError("Malformed resource telemetry")
        policy = resource.get("archive_budget_bytes")
        reserve = resource.get("disk_reserve_bytes")
        recorded = resource.get("archived_jsonl_bytes")
        actual_archived = sum(c.get("archived_line_bytes", 0) for c in chunks)
        if (
            type(policy) is not int
            or not 1024 <= policy <= jsonl_cap
            or (
                producer in ("v13_rolling_bounded", "v20_multiday_bounded")
                and policy != jsonl_cap
            )
            or type(reserve) is not int
            or not 0 <= reserve <= 10**12
            or type(recorded) is not int
            or recorded != actual_archived
            or recorded > policy
            or type(resource.get("disk_checks")) is not int
            or resource["disk_checks"] < total_messages
            or type(resource.get("peak_rss_bytes")) is not int
            or resource["peak_rss_bytes"] <= 0
        ):
            raise SoakAuditError(
                "Producer resource telemetry contradicts archived source"
            )
        for key in (
            "max_write_latency_ns",
            "total_write_latency_ns",
            "max_fsync_latency_ns",
        ):
            if type(resource.get(key)) is not int or resource[key] < 0:
                raise SoakAuditError("Invalid disk latency counter")
        if resource.get("min_available_disk_bytes") is not None and (
            type(resource.get("min_available_disk_bytes")) is not int
            or resource["min_available_disk_bytes"] < 0
        ):
            raise SoakAuditError("Invalid available disk telemetry")
    if (
        producer == "v20_multiday_bounded"
        and not synthetic
        and status == "CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH"
        and manifest["required_duration_seconds"] >= 60
    ):
        expected = manifest["required_duration_seconds"]
        if (
            len(connections) != 1
            or hb_per_conn[0] < int(expected * 0.85)
            or 0 not in hb_first_receipt
            or 0 not in hb_last_receipt
            or (hb_first_receipt[0] - start).total_seconds() > 12
            or (end - hb_last_receipt[0]).total_seconds() > 12
        ):
            raise SoakAuditError("Long-run capture heartbeat coverage insufficient")
    all_ready = all(
        len(seen_snapshots[i]) == 2
        and hb_per_conn[i] > 0
        and all(updates_per_conn[i][p] > 0 for p in PRODUCTS)
        for i in conn_by_id
    )
    no_faults = not faults and all(c["integrity_fault"] is False for c in connections)
    archive_complete = (
        status == "CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH"
        and no_faults
        and all_ready
        and len(connections) == 1
        and manifest.get("ready_for_research") is True
        and manifest.get("complete_sequence_per_connection") is True
        and manifest.get("all_connections_have_snapshots") is True
    )
    if (
        status == "CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH"
        and not archive_complete
    ):
        raise SoakAuditError(
            "Manifest mislabels incomplete/fragmented capture as gap-free"
        )
    if (
        status != "CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH"
        and manifest.get("ready_for_research") is not False
    ):
        raise SoakAuditError("Gapped/incomplete capture incorrectly admitted")

    def _book_summary(book):
        levels = [
            (side, format(price.normalize(), "f"), format(qty.normalize(), "f"))
            for side in ("bid", "offer")
            for price, qty in sorted(book[side].items())
        ]
        data = json.dumps(levels, separators=(",", ":"), ensure_ascii=True).encode()
        return {
            "best_bid": format(max(book["bid"]).normalize(), "f")
            if book["bid"]
            else None,
            "best_ask": format(min(book["offer"]).normalize(), "f")
            if book["offer"]
            else None,
            "bid_levels": len(book["bid"]),
            "ask_levels": len(book["offer"]),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    book_summaries = {p: _book_summary(books[p]) for p in PRODUCTS}
    return {
        "status": "INTERNALLY_REPLAYED_NO_KNOWN_GAP"
        if archive_complete
        else "ARCHIVED_BUT_NOT_ADMITTED",
        "archived_source_consistency_passed": True,
        "eligible_for_point_in_time_feature_research": archive_complete,
        "connection_count": len(connections),
        "trade_dedup_scope": "PER_CONNECTION_ONLY_NOT_CONTINUOUS_ACROSS_GAPS",
        "message_count": total_messages,
        "chunk_count": len(chunks),
        "long_run_heartbeat_receipt_max_gap_seconds": hb_max_gap_seconds.get(0)
        if producer == "v20_multiday_bounded" and not synthetic
        else None,
        "raw_text_bytes": total_raw,
        "per_product": {p: dict(counters[p]) for p in PRODUCTS},
        "books_final": book_summaries,
        "connection_sequences": {
            str(i): [first_sequence.get(i), last_seq.get(i)] for i in conn_by_id
        },
        "non_atomic_venue_reference_verified": False,
        "independent_market_truth_verified": False,
        "source_clock_sync_verified": False,
        "trading_execution_cost_verified": False,
        "executable_alpha_verified": False,
        "orders_enabled": False,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("archive", type=Path)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args()
    try:
        result = audit(args.archive)
    except (OSError, ValueError, TypeError, KeyError) as error:
        result = {
            "status": "REJECTED",
            "error": str(error)[:500],
            "eligible_for_point_in_time_feature_research": False,
            "orders_enabled": False,
            "executable_alpha_verified": False,
        }
    if args.report:
        args.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["status"] != "INTERNALLY_REPLAYED_NO_KNOWN_GAP":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
