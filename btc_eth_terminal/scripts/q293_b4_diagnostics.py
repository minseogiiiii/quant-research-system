"""B4.1 diagnostic receipts; allowlisted reasons, never raw data or error text."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
from pathlib import Path

from independent_soak_replay import strict_json

MAX_MANIFEST = 24_000_000
MAX_EVENTS = 512
MAX_LINE = 1024
ZERO = "0" * 64
SOURCE_ERRORS = {
    "NON_TEXT", "SEQUENCE_GAP", "CHANNEL", "BAD_BOOK_EVENT", "DUP_SNAPSHOT",
    "UPDATE_BEFORE_SNAPSHOT", "SIZE_CAP", "ARCHIVE_BUDGET_EXCEEDED",
    "DISK_RESERVE_EXCEEDED", "PARTIAL_DISK_WRITE",
}
ENGINE_REASONS = {
    "SOURCE_ENVELOPE_REJECTED", "SOURCE_REQUIRES_NEW_CONNECTION",
    "SOURCE_STOPPED", "SOURCE_TRANSPORT_LOST",
    "COLLECTOR_REPORTED_WEBSOCKET_SOURCE_GAP",
    "COLLECTOR_OBSERVATION_SOCKET_CLOSED",
    "NO_COLLECTOR_MESSAGES_WITHIN_STALENESS_BUDGET", "HEARTBEAT_STALE",
    "PRODUCT_L2_STALE_OR_CLOCK_ANOMALY", "LIVE_ADMISSION_REJECTED",
    "INDEPENDENT_SOURCE_WITNESS_FAILED", "LOCAL_OBSERVER_BACKPRESSURE_EXCEEDED",
    "INVALID_OBSERVER_TRANSPORT_CONTROL", "OBSERVER_SOCKET_FAILURE",
    "DUAL_UNTRUSTED_IPC", "DUAL_IPC_FAILED", "WAITING_FOR_DUAL_SNAPSHOT_AND_ACKS",
    "COLLECTOR_EXITED", "BOOK_WITNESS_NOT_READY", "PAIR_NOT_READY",
    "READY", "IPC_TIMEOUT", "IPC_EOF", "IPC_SCHEMA_REJECTED",
    "OBSERVER_EXITED", "OBSERVER_STARTUP_TIMEOUT", "OBSERVER_STARTING",
    "OBSERVATION_STALE", "NO_OBSERVATION", "IPC_OS_ERROR",
}
FINISH = {
    "DURATION_REACHED", "SIGTERM_QUARANTINE", "SIGINT_QUARANTINE",
    "DISK_FAILURE", "NONRECOVERABLE_STORAGE_LIMIT", "RECONNECT_BUDGET",
    "STARTUP_READY_TIMEOUT_QUARANTINE", "NO_WEBSOCKET",
}


def reason_class(reason):
    if not isinstance(reason, str):
        return "OTHER_REDACTED"
    return reason if reason in ENGINE_REASONS else (
        "SOURCE_ENVELOPE_REJECTED" if reason.startswith("SOURCE_ENVELOPE_REJECTED:")
        else "OBSERVER_SOCKET_FAILURE" if reason.startswith("OBSERVER_SOCKET_FAILURE:")
        else "OTHER_REDACTED")


def fault_class(reason):
    if not isinstance(reason, str):
        return "UNKNOWN_REDACTED"
    for name in SOURCE_ERRORS:
        if reason == "SOURCE:Error: " + name:
            return name
    if reason == "STALE_FEED":
        return reason
    if reason == "INJECTED_DISCONNECT_AFTER_COMMITTED_SOURCE":
        return "CONTROLLED_INJECTION"
    if re.fullmatch(r"CLOSE:[0-9]{4}", reason):
        return "WEBSOCKET_CLOSE"
    for prefix in ("ERROR:", "CONNECT:", "ARCHIVE_ERROR:", "SOCKET_CLOSE_ERROR:"):
        if reason.startswith(prefix):
            return prefix[:-1]
    if reason.startswith("SOURCE:SyntaxError:"):
        return "JSON_PARSE_ERROR"
    return "UNKNOWN_REDACTED"


def manifest_receipt(path):
    """Read final, quiescent manifest only. Do not trust an arbitrary fault string."""
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or not 0 < before.st_size <= MAX_MANIFEST):
                raise ValueError("UNSAFE_MANIFEST")
            raw = stream.read(MAX_MANIFEST + 1)
            after = os.fstat(stream.fileno())
            if (len(raw) != before.st_size or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns):
                raise ValueError("MANIFEST_CHANGED")
        m = strict_json(raw)
        if (m["format"] != "btc_eth_raw_soak_v10" or m["orders_enabled"] is not False
                or type(m["faults"]) is not list or len(m["faults"]) > 64
                or type(m["connections"]) is not list or len(m["connections"]) > 16):
            raise ValueError("MANIFEST_SCHEMA")
        faults = []
        for f in m["faults"]:
            cid = f["connection_id"]
            if cid is not None and (type(cid) is not int or not 0 <= cid < 16):
                raise ValueError("MANIFEST_CONNECTION")
            faults.append({"connection": cid, "class": fault_class(f["reason"])})
        finish = m.get("finish_reason")
        return {"status": "MANIFEST_READ", "manifest_sha256":
                hashlib.sha256(raw).hexdigest(), "faults": faults,
                "first_fault_class": faults[0]["class"] if faults else None,
                "finish_class": finish if finish in FINISH else "OTHER_REDACTED",
                "connections": len(m["connections"]),
                "network_root_cause_proven": False}
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "MANIFEST_UNAVAILABLE_OR_REJECTED",
                "network_root_cause_proven": False}


def canonical(row):
    return json.dumps(row, sort_keys=True, separators=(",", ":")).encode()


class ObserverJournal:
    """First and subsequent changed observations persisted before invalidation."""
    def __init__(self, path):
        self.fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                          os.O_NOFOLLOW, 0o600)
        self.lock = threading.Lock()
        self.count = 0
        self.previous = ZERO
        self.last = None
        self.failed = False

    def record(self, reason, connection, sequence):
        reason = reason_class(reason)
        conn = (connection if type(connection) is int
                and 0 <= connection < 1000 else None)
        seq = sequence if type(sequence) is int and -1 <= sequence < 10**9 else None
        with self.lock:
            if self.failed:
                raise OSError("OBSERVER_JOURNAL_FAILED")
            key = (reason, conn)
            if key == self.last:
                return
            if self.count >= MAX_EVENTS:
                raise ValueError("OBSERVER_JOURNAL_LIMIT")
            row = {"index": self.count, "mono_ns": time.monotonic_ns(),
                   "reason": reason, "connection": conn, "sequence": seq,
                   "previous": self.previous}
            digest = hashlib.sha256(canonical(row)).hexdigest()
            raw = canonical({**row, "sha256": digest}) + b"\n"
            try:
                if len(raw) > MAX_LINE or os.write(self.fd, raw) != len(raw):
                    raise OSError("OBSERVER_JOURNAL_WRITE")
                os.fsync(self.fd)
            except OSError:
                self.failed = True
                raise
            self.previous = digest
            self.count += 1
            self.last = key

    def close(self):
        with self.lock:
            os.close(self.fd)


def observer_receipt(path):
    rows = []
    previous = ZERO
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            st = os.fstat(stream.fileno())
            if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1
                    or st.st_mode & 0o077 or st.st_size > MAX_LINE * MAX_EVENTS):
                raise ValueError("OBSERVER_JOURNAL_FILE")
            while raw := stream.readline(MAX_LINE + 1):
                if (len(raw) > MAX_LINE or not raw.endswith(b"\n")
                        or len(rows) >= MAX_EVENTS):
                    raise ValueError("OBSERVER_JOURNAL_TRUNCATED")
                row = strict_json(raw)
                digest = row.pop("sha256")
                if (set(row) != {"index", "mono_ns", "reason", "connection",
                                 "sequence", "previous"}
                        or row["index"] != len(rows) or row["previous"] != previous
                        or row["reason"] not in ENGINE_REASONS | {"OTHER_REDACTED"}
                        or hashlib.sha256(canonical(row)).hexdigest() != digest):
                    raise ValueError("OBSERVER_JOURNAL_CHAIN")
                rows.append({k: row[k] for k in
                             ("mono_ns", "reason", "connection", "sequence")})
                previous = digest
        return {"status": "VERIFIED", "events": rows, "terminal_sha256": previous}
    except (OSError, ValueError, TypeError, KeyError):
        return {"status": "UNAVAILABLE_OR_REJECTED", "verified_prefix": rows}
