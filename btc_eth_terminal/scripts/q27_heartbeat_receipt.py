"""Q27: publish only schema-validated, bounded heartbeat guard diagnostics.

The read-only Node preload's PRIVATE trace is owned by the collector. This
module accepts fixed enumerated fields, counters and booleans only. It never
releases source frames, endpoint, close reason, error text, prices or tokens.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

SCHEMA = "q27_heartbeat_guard_event_v1"
MAX_BYTES = 55_000
MAX_EVENTS = 400
EVENTS = frozenset({
    "GUARD_START", "SOCKET_CONSTRUCTED", "SOCKET_OPEN",
    "FIRST_HEARTBEAT", "SOCKET_ERROR", "SOCKET_CLOSE",
    "FIRST_HEARTBEAT_TIMEOUT", "HEARTBEAT_STALE_FORCE_CLOSE",
    "GUARD_CLOSE_THROW", "PROCESS_EXIT",
})
KEYS = frozenset({
    "schema", "index", "event", "socket_index", "elapsed_ms",
    "socket_age_ms", "last_heartbeat_age_ms", "heartbeat_count",
    "other_message_count", "timeout_threshold_ms", "startup_grace_ms",
    "interventions_total", "synthetic_fixture", "real_orders_enabled",
    "native_ping_pong_visible",
})
PUBLIC_RECENT_LIMIT = 12


class Q27TraceRejected(ValueError):
    """A malformed diagnostic is never counted as a passing heartbeat run."""


def _nonnegative(value: object, max_value: int = 2_000_000_000) -> bool:
    return type(value) is int and 0 <= value <= max_value


def read_trace(
    path: Path, *, preserve_crash_prefix: bool = False
) -> list[dict]:
    """Strict normal acceptance or incomplete committed-prefix forensics.

    The forensic path is NEVER used as proof of source continuity.
    Unknown fields and malformed complete records still fail closed.
    """
    source = Path(path)
    st = source.lstat()
    if (
        not stat.S_ISREG(st.st_mode)
        or st.st_uid != os.getuid()
        or st.st_mode & 0o077
        or st.st_nlink != 1
        or not 0 < st.st_size <= MAX_BYTES
    ):
        raise Q27TraceRejected("PRIVATE_TRACE_PERMISSIONS_OR_BUDGET_FAILED")
    fd = os.open(
        source, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        opened = os.fstat(fd)
        if (
            (opened.st_dev, opened.st_ino, opened.st_size)
            != (st.st_dev, st.st_ino, st.st_size)
            or opened.st_nlink != 1
            or not stat.S_ISREG(opened.st_mode)
        ):
            raise Q27TraceRejected("PRIVATE_TRACE_CHANGED_DURING_READ")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) != st.st_size:
            raise Q27TraceRejected("PRIVATE_TRACE_PARTIAL_OR_MUTATED")
        if not raw.endswith(b"\n"):
            if not preserve_crash_prefix:
                raise Q27TraceRejected("PRIVATE_TRACE_PARTIAL_OR_MUTATED")
            raw = raw[:raw.rfind(b"\n") + 1]
            if not raw:
                raise Q27TraceRejected("NO_COMMITTED_GUARD_TRACE_EVENTS")
    finally:
        os.close(fd)
    if source.lstat().st_size != st.st_size:
        raise Q27TraceRejected("PRIVATE_TRACE_MUTATED")
    lines = raw.splitlines()
    if not 2 <= len(lines) <= MAX_EVENTS:
        raise Q27TraceRejected("PRIVATE_TRACE_EVENT_COUNT_INVALID")
    events = []
    prev_elapsed = -1
    prev_interventions = 0
    current_socket = -1
    last_per_socket = {}
    constructed = set()
    for n, line in enumerate(lines):
        if len(line) > 2048:
            raise Q27TraceRejected("PRIVATE_TRACE_OVERSIZED_EVENT")
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise Q27TraceRejected("PRIVATE_TRACE_MALFORMED_JSON") from exc
        if (
            type(event) is not dict
            or set(event) != KEYS
            or event["schema"] != SCHEMA
            or event["index"] != n
            or event["event"] not in EVENTS
            or event["synthetic_fixture"] not in (True, False)
            or type(event["synthetic_fixture"]) is not bool
            or event["real_orders_enabled"] is not False
            or event["native_ping_pong_visible"] is not False
            or event["timeout_threshold_ms"] != 4500
            or type(event["timeout_threshold_ms"]) is not int
            or event["startup_grace_ms"] != 9000
            or type(event["startup_grace_ms"]) is not int
        ):
            raise Q27TraceRejected("PRIVATE_TRACE_SCHEMA_OR_LEAKAGE_REJECTED")
        for key in (
            "elapsed_ms", "heartbeat_count", "other_message_count",
            "interventions_total",
        ):
            if not _nonnegative(event[key]):
                raise Q27TraceRejected("PRIVATE_TRACE_COUNTER_INVALID")
        for key in ("socket_age_ms", "last_heartbeat_age_ms"):
            if event[key] is not None and not _nonnegative(event[key]):
                raise Q27TraceRejected("PRIVATE_TRACE_EVENT_AGE_INVALID")
        idx = event["socket_index"]
        if idx is not None and (
            not _nonnegative(idx, 64)
            or idx < current_socket
        ):
            raise Q27TraceRejected("PRIVATE_TRACE_SOCKET_INDEX_INVALID")
        if (
            event["elapsed_ms"] < prev_elapsed
            or event["interventions_total"] < prev_interventions
        ):
            raise Q27TraceRejected("PRIVATE_TRACE_NONMONOTONE_CLOCK_OR_COUNT")
        if idx is not None:
            if event["event"] == "SOCKET_CONSTRUCTED":
                if idx != current_socket + 1 or idx in constructed:
                    raise Q27TraceRejected("PRIVATE_TRACE_SOCKET_REUSED")
                constructed.add(idx)
            elif idx not in constructed:
                raise Q27TraceRejected("PRIVATE_TRACE_SOCKET_NOT_CONSTRUCTED")
            if idx in last_per_socket:
                previous = last_per_socket[idx]
                if (
                    event["heartbeat_count"] < previous[0]
                    or event["other_message_count"] < previous[1]
                ):
                    raise Q27TraceRejected("PRIVATE_TRACE_SOCKET_COUNT_REGRESSION")
            last_per_socket[idx] = (
                event["heartbeat_count"], event["other_message_count"]
            )
            current_socket = idx
        elif event["event"] not in ("GUARD_START",):
            raise Q27TraceRejected("PRIVATE_TRACE_SOCKET_INDEX_MISSING")
        prev_elapsed = event["elapsed_ms"]
        prev_interventions = event["interventions_total"]
        events.append(event)
    if (
        events[0]["event"] != "GUARD_START"
        or not preserve_crash_prefix
        and events[-1]["event"] != "PROCESS_EXIT"
        or not constructed
    ):
        raise Q27TraceRejected("PRIVATE_TRACE_MISSING_START_OR_EXIT")
    triggers = sum(
        event["event"] in (
            "FIRST_HEARTBEAT_TIMEOUT", "HEARTBEAT_STALE_FORCE_CLOSE",
        )
        for event in events
    )
    if triggers != events[-1]["interventions_total"]:
        raise Q27TraceRejected("PRIVATE_TRACE_INTERVENTION_MISMATCH")
    return events


def summarize(events: list[dict]) -> dict:
    timeouts = [
        event for event in events
        if event["event"] in (
            "FIRST_HEARTBEAT_TIMEOUT", "HEARTBEAT_STALE_FORCE_CLOSE",
        )
    ]
    firsts = [e for e in events if e["event"] == "FIRST_HEARTBEAT"]
    created = [e for e in events if e["event"] == "SOCKET_CONSTRUCTED"]
    return {
        "schema": "q27_heartbeat_guard_public_receipt_v1",
        "trace_schema_verified": True,
        "classification": "READ_ONLY_HEARTBEAT_GUARD_OBSERVATION",
        "synthetic_fixture": events[-1]["synthetic_fixture"],
        "opened_sockets_constructed": len(created),
        "sockets_with_first_heartbeat": len(firsts),
        "heartbeat_timeout_events": len(timeouts),
        "interventions_total": events[-1]["interventions_total"],
        "last_heartbeat_count": events[-1]["heartbeat_count"],
        "last_nonheartbeat_message_count": events[-1]["other_message_count"],
        "first_heartbeat_timeout_events": sum(
            e["event"] == "FIRST_HEARTBEAT_TIMEOUT" for e in timeouts
        ),
        "established_heartbeat_stale_events": sum(
            e["event"] == "HEARTBEAT_STALE_FORCE_CLOSE" for e in timeouts
        ),
        "close_failure_events": sum(
            e["event"] == "GUARD_CLOSE_THROW" for e in events
        ),
        "observed_event_count": len(events),
        "collector_stop_observed": events[-1]["event"] == "PROCESS_EXIT",
        "recent_bounded_events": events[-PUBLIC_RECENT_LIMIT:],
        "native_ping_pong_control_frames_available": False,
        "heartbeat_gap_was_real_coinbase_proven": False,
        "continuous_5h_proven": False,
        "execution_authorized": False,
        "real_orders_enabled": False,
    }


def build_receipt(source: Path) -> dict:
    return summarize(read_trace(source))


def publish_receipt(private: Path, public: Path) -> dict:
    result = build_receipt(private)
    public = Path(public)
    fd = os.open(
        public, os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    with os.fdopen(fd, "w") as stream:
        json.dump(result, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return result
