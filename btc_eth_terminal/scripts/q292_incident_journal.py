"""Q29.2-B: independent, bounded, privacy-safe incident journal.

Polling evidence only: no claim of gapless capture, physical root cause, or
order admission. Run on a separate process from the collector and supervisor.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from q23_redacted_diagnostics import read_private_status, redacted_snapshot

MAX_RECORDS = 40_000
MAX_LINE = 2048
MAX_BYTES = MAX_RECORDS * MAX_LINE
ZERO = "0" * 64
STATES = {"HEALTHY", "STALE", "RECONNECTING", "WAITING_FOR_SNAPSHOT",
          "QUARANTINED", "STOPPED", "UNKNOWN"}
EVENTS = {"START", "SAMPLE", "UNAVAILABLE", "STOP"}
KEYS = {"schema", "index", "mono_ns", "event", "previous_sha256",
        "source", "real_orders_enabled", "sha256"}
SOURCE_KEYS = {"status", "session_sha256", "connection", "gaps", "messages",
               "sequence", "heartbeats", "receiver_age_ms", "ready"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("ascii")


def number(value, *, signed=False):
    return value if type(value) is int and (
        (-2**63 if signed else 0) <= value < 2**63) else None


def safe_source(health, metrics, http, metrics_http, now_ms=None):
    s = redacted_snapshot(health, metrics, [], now_ms=now_ms)
    # Never publish an unchecked payload, reason string or source identifier.
    ready = (
        http == metrics_http == 200
        and health.get("mode") == "LIVE_SOURCE"
        and health.get("status") == "HEALTHY"
        and health.get("operationally_healthy") is True
        and health.get("live_feed") is True
        and health.get("trading_enabled") is False
        and health.get("execution_authorized") is False
        and health.get("q25_isolation_schema") == "q25_source_epoch_isolation_v1"
        and health.get("snapshots") == ["BTC-USD", "ETH-USD"]
        and health.get("subscriptions") == ["heartbeats", "level2", "market_trades"]
        and metrics.get("monitor_transport") == "UNIX_SOCKET"
        and metrics.get("trading_enabled") is False
        and metrics.get("execution_authorized") is False
        and s["source_session_sha256"] is not None
        and all(s[k] is not None for k in ("source_connection_id",
                "source_gap_count", "source_messages", "source_sequence",
                "heartbeat_count", "last_receiver_age_ms"))
        and s["heartbeat_count"] > 0
        and s["source_messages"] > 0
        and s["source_sequence"] < s["source_messages"]
        and 0 <= s["last_receiver_age_ms"] <= 15_000
    )
    return {
        "status": s["source_status"],
        "session_sha256": s["source_session_sha256"],
        "connection": s["source_connection_id"],
        "gaps": s["source_gap_count"],
        "messages": s["source_messages"],
        "sequence": s["source_sequence"],
        "heartbeats": s["heartbeat_count"],
        "receiver_age_ms": number(s["last_receiver_age_ms"], signed=True),
        "ready": ready,
    }


def validate_source(source):
    if type(source) is not dict or set(source) != SOURCE_KEYS:
        raise ValueError("JOURNAL_SOURCE_SCHEMA")
    if source["status"] not in STATES or type(source["ready"]) is not bool:
        raise ValueError("JOURNAL_SOURCE_STATE")
    digest = source["session_sha256"]
    if digest is not None and (type(digest) is not str or
                               not re.fullmatch("[0-9a-f]{64}", digest)):
        raise ValueError("JOURNAL_SOURCE_DIGEST")
    for k in ("connection", "gaps", "messages", "sequence", "heartbeats",
              "receiver_age_ms"):
        if source[k] is not None and number(
                source[k], signed=k == "receiver_age_ms") is None:
            raise ValueError("JOURNAL_SOURCE_COUNTER")
    if source["ready"] and (
        source["status"] != "HEALTHY" or digest is None
        or any(source[k] is None for k in SOURCE_KEYS - {
            "status", "session_sha256", "ready"})
        or source["heartbeats"] <= 0
        or source["messages"] <= source["sequence"]
        or not 0 <= source["receiver_age_ms"] <= 15_000
    ):
        raise ValueError("JOURNAL_FALSE_READY")


class Journal:
    def __init__(self, path):
        path = Path(path)
        parent = path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid():
            raise ValueError("JOURNAL_UNSAFE_DIRECTORY")
        if parent.st_mode & 0o077:
            raise ValueError("JOURNAL_UNSAFE_DIRECTORY")
        self.fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                          getattr(os, "O_NOFOLLOW", 0), 0o600)
        self.index = self.used = 0
        self.previous = ZERO
        self.last_ns = -1
        self.failed = False
        self.stopped = False
        # Persist the directory entry as well as subsequent records.
        try:
            dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            os.close(self.fd)
            raise

    def append(self, event, source=None, *, mono_ns=None):
        if self.failed or self.stopped:
            raise ValueError("JOURNAL_NOT_WRITABLE")
        now = time.monotonic_ns() if mono_ns is None else mono_ns
        if (event not in EVENTS or (event == "START") != (self.index == 0)
                or (event == "SAMPLE") != (source is not None)
                or number(now) is None or now <= self.last_ns):
            raise ValueError("JOURNAL_EVENT_ORDER")
        if source is not None:
            validate_source(source)
        row = {"schema": "q292_incident_journal_v1", "index": self.index,
               "mono_ns": now, "event": event, "previous_sha256": self.previous,
               "source": source, "real_orders_enabled": False}
        row["sha256"] = hashlib.sha256(canonical(row)).hexdigest()
        raw = canonical(row) + b"\n"
        if (self.index >= MAX_RECORDS or len(raw) > MAX_LINE
                or self.used + len(raw) > MAX_BYTES):
            self.failed = True
            raise ValueError("JOURNAL_BUDGET_EXHAUSTED")
        try:
            view = memoryview(raw)
            while view:
                n = os.write(self.fd, view)
                if n <= 0:
                    raise OSError("JOURNAL_SHORT_WRITE")
                view = view[n:]
            os.fsync(self.fd)
        except OSError:
            self.failed = True
            raise
        self.index += 1
        self.used += len(raw)
        self.last_ns = now
        self.previous = row["sha256"]
        self.stopped = event == "STOP"
        return row

    def close(self):
        os.close(self.fd)


def read_journal(path):
    """Validate bounded committed records, retaining only a torn final tail.

    A corrupted complete record fails closed. Hash chain detects accidental
    mutation, not an adversary who can rewrite the whole file and chain.
    """
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                 | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        st = os.fstat(stream.fileno())
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or st.st_nlink != 1 or st.st_mode & 0o077
                or not 0 < st.st_size <= MAX_BYTES):
            raise ValueError("JOURNAL_UNSAFE_FILE")
        rows, previous, last_ns, torn = [], ZERO, -1, False
        while raw := stream.readline(MAX_LINE + 1):
            if len(raw) > MAX_LINE:
                raise ValueError("JOURNAL_LINE_LIMIT")
            if not raw.endswith(b"\n"):
                torn = True
                break
            row = json.loads(raw)
            if type(row) is not dict or set(row) != KEYS:
                raise ValueError("JOURNAL_RECORD_SCHEMA")
            digest = row.pop("sha256")
            event, now = row["event"], row["mono_ns"]
            if (row["schema"] != "q292_incident_journal_v1"
                    or type(row["index"]) is not int or row["index"] != len(rows)
                    or row["previous_sha256"] != previous
                    or digest != hashlib.sha256(canonical(row)).hexdigest()
                    or row["real_orders_enabled"] is not False
                    or event not in EVENTS or (event == "START") != (not rows)
                    or (event == "SAMPLE") != (row["source"] is not None)
                    or number(now) is None or now <= last_ns
                    or (rows and rows[-1]["event"] == "STOP")):
                raise ValueError("JOURNAL_CHAIN_OR_ORDER")
            if row["source"] is not None:
                validate_source(row["source"])
            row["sha256"] = digest
            rows.append(row)
            previous, last_ns = digest, now
            if len(rows) > MAX_RECORDS:
                raise ValueError("JOURNAL_RECORD_LIMIT")
        end = os.fstat(stream.fileno())
        if (st.st_size, st.st_mtime_ns) != (end.st_size, end.st_mtime_ns):
            raise ValueError("JOURNAL_CHANGED_DURING_READ")
    if not rows or (torn and rows[-1]["event"] == "STOP"):
        raise ValueError("JOURNAL_NO_COMMITTED_START_OR_INVALID_TAIL")
    return rows, torn


def audit(rows, torn):
    """Track observed epochs; polling cannot prove precise outage boundaries."""
    gaps = []
    last = None
    unavailable = 0
    anomalies = 0
    ready_samples = 0
    seen_sessions = set()
    for row in rows:
        if row["event"] == "UNAVAILABLE":
            unavailable += 1
        s = row["source"]
        if s is None or not s["ready"]:
            continue
        ready_samples += 1
        if last is not None:
            a, b = last["source"], s
            changed = a["session_sha256"] != b["session_sha256"]
            delta = b["gaps"] - a["gaps"]
            if changed or delta:
                valid = (changed and delta > 0 and
                         b["connection"] > a["connection"] and
                         b["session_sha256"] not in seen_sessions and
                         b["messages"] >= a["messages"])
                gaps.append({"previous_index": last["index"],
                             "new_index": row["index"], "gap_delta": delta,
                             "transition_consistent": valid,
                             "historical_gap_recovered": False})
                anomalies += int(not valid)
            elif (b["connection"] != a["connection"]
                  or any(b[k] < a[k] for k in (
                      "messages", "sequence", "heartbeats"))):
                anomalies += 1
        seen_sessions.add(s["session_sha256"])
        last = row
    return {"schema": "q292_incident_journal_audit_v1",
            "records": len(rows), "terminal_sha256": rows[-1]["sha256"],
            "clean_stop": rows[-1]["event"] == "STOP" and not torn,
            "torn_tail": torn, "unavailable_samples": unavailable,
            "ready_samples": ready_samples,
            "observed_epoch_transitions": gaps, "anomalies": anomalies,
            "physical_root_cause_proven": False,
            "zero_gap_certified": False, "resilience_certified": False,
            "real_orders_enabled": False}


class JournalProcess:
    """Owner-side lifecycle adapter; collector and journal are separate PIDs.

    Evidence is diagnostic-only; a failed journal prevents opt-in strict
    acceptance but a healthy journal cannot waive any existing capture gate.
    """

    def __init__(self, work, public, seconds):
        self.directory = work.parent / (work.name + ".incident")
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / "journal.jsonl"
        self.public = public / "q292_journal_audit.json"
        self.process = subprocess.Popen([
            sys.executable, str(Path(__file__).resolve()),
            "--socket", str(work / "v36_monitor.sock"),
            "--journal", str(self.path),
            "--duration", str(seconds + 90),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
           start_new_session=True)
        self.result = None

    def check(self):
        if self.process.poll() is not None:
            raise ValueError("Q292_INDEPENDENT_JOURNAL_EXITED")

    def finish(self):
        if self.result is not None:
            return self.result
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        result = audit(*read_journal(self.path))
        result["monitor_exit_code"] = self.process.returncode
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.public, flags, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(result) + b"\\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.result = result
        return result

    def require_clean(self):
        result = self.finish()
        if (not result["clean_stop"] or result["monitor_exit_code"] != 0
                or result["ready_samples"] < 1 or result["anomalies"] != 0):
            raise ValueError("Q292_JOURNAL_NOT_CLEAN")
        return result


def monitor(sock, path, period, duration):
    stop = False

    def request_stop(_sig, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    journal = Journal(path)
    try:
        journal.append("START")
        end = time.monotonic() + duration
        while not stop and time.monotonic() < end:
            started = time.monotonic()
            try:
                h, health = read_private_status(sock, "/api/v36/health")
                m, metrics = read_private_status(sock, "/api/v36/metrics")
                source = safe_source(health, metrics, h, m)
                validate_source(source)
            except (OSError, ValueError, TypeError, KeyError):
                journal.append("UNAVAILABLE")
            else:
                journal.append("SAMPLE", source)
            time.sleep(max(0, period - (time.monotonic() - started)))
        journal.append("STOP")
    finally:
        journal.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--duration", type=int, required=True)
    parser.add_argument("--period", type=float, default=2)
    args = parser.parse_args()
    if not 1 <= args.period <= 10 or not 1 <= args.duration <= 19_000:
        parser.error("monitor duration/period outside bounded policy")
    monitor(args.socket, args.journal, args.period, args.duration)


if __name__ == "__main__":
    main()
