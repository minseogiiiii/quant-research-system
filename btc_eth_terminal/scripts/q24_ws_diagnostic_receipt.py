"""Q24 P0-A: strict redaction of Node WebSocket close/heartbeat diagnostics.

No traffic, prices, WebSocket URL, close reason, error strings or credentials
are accepted in the private trace format or released in the CI receipt.
The native WHATWG WebSocket API does NOT expose ping/pong frames.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

MAX_TRACE_BYTES = 190_000
MAX_EVENTS = 480
MAX_PUBLISHED = 16
EVENTS = frozenset({
    "TRACE_START", "SOCKET_CONSTRUCTED", "SOCKET_OPEN",
    "SOCKET_ERROR", "SOCKET_CLOSE", "LOCAL_CLOSE_REQUEST",
    "PROGRESS", "PROCESS_EXIT",
})
KEYS = frozenset({
    "schema", "seq", "event", "connection_index", "elapsed_ms",
    "connection_uptime_ms", "message_count", "heartbeat_count",
    "last_message_age_ms", "last_heartbeat_age_ms", "close_code",
    "was_clean", "local_close_requested", "reconnect_delay_ms",
    "ping_pong_control_frames_visible", "real_orders_enabled",
})


class Q24TraceRejected(ValueError):
    pass


def _optional_int(value, bound=259_200_000):
    return value is None or (
        type(value) is int and 0 <= value <= bound
    )


def read_private_trace(path: Path) -> list[dict]:
    path = Path(path)
    st = path.lstat()
    if (
        not stat.S_ISREG(st.st_mode)
        or st.st_nlink != 1
        or st.st_uid != os.getuid()
        or st.st_mode & 0o077
        or not 0 < st.st_size <= MAX_TRACE_BYTES
    ):
        raise Q24TraceRejected("PRIVATE_DIAGNOSTIC_FILE_UNSAFE_OR_UNBOUNDED")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (
            (opened.st_dev, opened.st_ino, opened.st_size)
            != (st.st_dev, st.st_ino, st.st_size)
            or opened.st_nlink != 1
            or not stat.S_ISREG(opened.st_mode)
        ):
            raise Q24TraceRejected("TRACE_REPLACED_DURING_OPEN")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_TRACE_BYTES + 1)
        if len(raw) != st.st_size or not raw.endswith(b"\n"):
            raise Q24TraceRejected("TRUNCATED_PRIVATE_WS_TRACE")
    finally:
        os.close(fd)
    if path.lstat().st_size != st.st_size:
        raise Q24TraceRejected("TRACE_CHANGED_DURING_READ")
    lines = raw.splitlines()
    if not 1 <= len(lines) <= MAX_EVENTS:
        raise Q24TraceRejected("TRACE_EVENT_BUDGET_EXCEEDED")
    items = []
    prev_elapsed = -1
    previous_index = -1
    for n, line in enumerate(lines):
        if len(line) > 2048:
            raise Q24TraceRejected("TRACE_EVENT_OVERSIZED")
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise Q24TraceRejected("MALFORMED_WS_TRACE_EVENT") from exc
        if (
            not isinstance(event, dict)
            or set(event) != KEYS
            or event["schema"] != "q24_ws_close_event_v1"
            or event["seq"] != n
            or event["event"] not in EVENTS
            or event["ping_pong_control_frames_visible"] is not False
            or event["real_orders_enabled"] is not False
            or event["local_close_requested"] not in (True, False)
            or type(event["local_close_requested"]) is not bool
            or event["was_clean"] is not None
            and type(event["was_clean"]) is not bool
        ):
            raise Q24TraceRejected("UNTRUSTED_OR_UNREDACTED_WS_TRACE_EVENT")
        for key in (
            "elapsed_ms", "connection_uptime_ms", "message_count",
            "heartbeat_count", "last_message_age_ms",
            "last_heartbeat_age_ms", "reconnect_delay_ms",
        ):
            if not _optional_int(event[key], 2_000_000_000):
                raise Q24TraceRejected("UNSAFE_WS_TRACE_COUNTER")
        idx = event["connection_index"]
        if not _optional_int(idx, 20) or event["elapsed_ms"] is None:
            raise Q24TraceRejected("INVALID_WS_CONNECTION_INDEX_OR_CLOCK")
        if (
            event["elapsed_ms"] < prev_elapsed
            or idx is not None and idx < previous_index
            or event["message_count"] < event["heartbeat_count"]
        ):
            raise Q24TraceRejected("NONMONOTONE_OR_INCONSISTENT_WS_TRACE")
        code = event["close_code"]
        if code is not None and (
            type(code) is not int or not 1000 <= code <= 4999
        ):
            raise Q24TraceRejected("INVALID_REDACTED_CLOSE_CODE")
        if event["event"] != "SOCKET_CLOSE" and (
            code is not None or event["was_clean"] is not None
        ):
            raise Q24TraceRejected("CLOSE_METADATA_ON_NON_CLOSE_EVENT")
        prev_elapsed = event["elapsed_ms"]
        if idx is not None:
            previous_index = idx
        items.append(event)
    if items[0]["event"] != "TRACE_START":
        raise Q24TraceRejected("MISSING_WS_TRACE_START")
    if not any(x["event"] == "SOCKET_CONSTRUCTED" for x in items):
        raise Q24TraceRejected("NO_TRACED_WEBSOCKET_CONSTRUCTION")
    return items


def summarize_trace(events: list[dict]) -> dict:
    """Classify what was OBSERVED, not an unproven network root cause."""
    closings = [x for x in events if x["event"] == "SOCKET_CLOSE"]
    errors = [x for x in events if x["event"] == "SOCKET_ERROR"]
    created = [x for x in events if x["event"] == "SOCKET_CONSTRUCTED"]
    opens = [x for x in events if x["event"] == "SOCKET_OPEN"]
    local_closes = [x for x in events if x["event"] == "LOCAL_CLOSE_REQUEST"]
    last_close = closings[-1] if closings else None
    last_error = errors[-1] if errors else None
    if last_close is None:
        kind = "NO_WEBSOCKET_CLOSE_EVENT_OBSERVED"
    elif last_close["local_close_requested"]:
        kind = "LOCAL_CLOSE_REQUEST_OBSERVED"
    elif last_close["close_code"] == 1006:
        kind = "ABNORMAL_WEBSOCKET_CLOSE_1006_OBSERVED"
    else:
        kind = "WEBSOCKET_CLOSE_WITHOUT_LOCAL_REQUEST_OBSERVED"
    if last_error and last_close and (
        last_error["connection_index"] == last_close["connection_index"]
        and last_error["elapsed_ms"] <= last_close["elapsed_ms"]
    ):
        kind = "WEBSOCKET_ERROR_PRECEDED_CLOSE_OBSERVED"
    return {
        "schema": "q24_p0a_public_close_forensics_v1",
        "classification": "SOURCE_TRANSPORT_DIAGNOSTICS_ONLY",
        "observed_close_class": kind,
        "observed_connections": len(created),
        "opened_connections": len(opens),
        "close_events": len(closings),
        "local_close_requests": len(local_closes),
        "websocket_error_events": len(errors),
        "last_close_code": last_close["close_code"] if last_close else None,
        "last_close_was_clean": last_close["was_clean"] if last_close else None,
        "last_close_local_request": (
            last_close["local_close_requested"] if last_close else None
        ),
        "last_close_heartbeat_age_ms": (
            last_close["last_heartbeat_age_ms"] if last_close else None
        ),
        "last_close_message_age_ms": (
            last_close["last_message_age_ms"] if last_close else None
        ),
        "last_reconnect_delay_ms": next((
            x["reconnect_delay_ms"] for x in reversed(created)
            if x["reconnect_delay_ms"] is not None
        ), None),
        "last_reported_heartbeat_count": events[-1]["heartbeat_count"],
        "last_reported_message_count": events[-1]["message_count"],
        "events_observed": len(events),
        "probe_exited": events[-1]["event"] == "PROCESS_EXIT",
        "recent_sanitized_events": events[-MAX_PUBLISHED:],
        "raw_websocket_payloads_saved": False,
        "error_messages_or_close_reasons_saved": False,
        "websocket_endpoint_or_ip_saved": False,
        "native_ping_pong_control_frame_observation_available": False,
        "root_network_cause_proven": False,
        "uninterrupted_five_hour_source_certified": False,
        "execution_authorized": False,
        "real_orders_enabled": False,
    }


def build_receipt(private_path: Path) -> dict:
    data = summarize_trace(read_private_trace(private_path))
    data["trace_schema_verified"] = True
    return data


def publish_receipt(private: Path, public: Path) -> dict:
    """Create ONLY redacted JSON; never copy raw private trace."""
    result = build_receipt(private)
    public = Path(public)
    if public.is_symlink() or public.exists():
        raise Q24TraceRejected("PUBLIC_DIAGNOSTIC_RECEIPT_ALREADY_EXISTS")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(
        os, "O_NOFOLLOW", 0
    )
    fd = os.open(public, flags, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(result, output, sort_keys=True, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    return result
