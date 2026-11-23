"""Q28: privacy-safe classification of completed WebSocket/guard trace events.

Called ONLY after collector termination. Crash mode may preserve a prefix of
newline-committed, schema-validated records when SIGTERM/SIGKILL prevented the
normal PROCESS_EXIT line. Such a prefix is forensic evidence ONLY, and NEVER
proof of good market continuity, live-source acceptance, or trading safety.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from q24_ws_diagnostic_receipt import (
    Q24TraceRejected,
    read_private_trace,
)
from q27_heartbeat_receipt import (
    Q27TraceRejected,
    read_trace,
)

ALLOWED_ERROR_CODES = frozenset({
    "PRIVATE_DIAGNOSTIC_FILE_UNSAFE_OR_UNBOUNDED",
    "TRACE_REPLACED_DURING_OPEN",
    "TRUNCATED_PRIVATE_WS_TRACE",
    "NO_COMMITTED_WS_TRACE_EVENTS",
    "TRACE_CHANGED_DURING_READ",
    "TRACE_EVENT_BUDGET_EXCEEDED",
    "TRACE_EVENT_OVERSIZED",
    "MALFORMED_WS_TRACE_EVENT",
    "UNTRUSTED_OR_UNREDACTED_WS_TRACE_EVENT",
    "UNSAFE_WS_TRACE_COUNTER",
    "INVALID_WS_CONNECTION_INDEX_OR_CLOCK",
    "NONMONOTONE_OR_INCONSISTENT_WS_TRACE",
    "INVALID_REDACTED_CLOSE_CODE",
    "CLOSE_METADATA_ON_NON_CLOSE_EVENT",
    "MISSING_WS_TRACE_START",
    "NO_TRACED_WEBSOCKET_CONSTRUCTION",
    "PRIVATE_TRACE_PERMISSIONS_OR_BUDGET_FAILED",
    "PRIVATE_TRACE_CHANGED_DURING_READ",
    "PRIVATE_TRACE_PARTIAL_OR_MUTATED",
    "NO_COMMITTED_GUARD_TRACE_EVENTS",
    "PRIVATE_TRACE_MUTATED",
    "PRIVATE_TRACE_EVENT_COUNT_INVALID",
    "PRIVATE_TRACE_OVERSIZED_EVENT",
    "PRIVATE_TRACE_MALFORMED_JSON",
    "PRIVATE_TRACE_SCHEMA_OR_LEAKAGE_REJECTED",
    "PRIVATE_TRACE_COUNTER_INVALID",
    "PRIVATE_TRACE_EVENT_AGE_INVALID",
    "PRIVATE_TRACE_SOCKET_INDEX_INVALID",
    "PRIVATE_TRACE_NONMONOTONE_CLOCK_OR_COUNT",
    "PRIVATE_TRACE_SOCKET_REUSED",
    "PRIVATE_TRACE_SOCKET_NOT_CONSTRUCTED",
    "PRIVATE_TRACE_SOCKET_COUNT_REGRESSION",
    "PRIVATE_TRACE_SOCKET_INDEX_MISSING",
    "PRIVATE_TRACE_MISSING_START_OR_EXIT",
    "PRIVATE_TRACE_INTERVENTION_MISMATCH",
})


def _safe_failure(exc: Exception) -> str:
    code = str(exc)
    if code in ALLOWED_ERROR_CODES:
        return code
    return "PRIVATE_TRACE_UNAVAILABLE_OR_INVALID"


def _read_one(path: Path, kind: str) -> tuple[list[dict], str]:
    """No fallback to unvalidated JSON, no raw snippets, URLs or reasons."""
    try:
        if kind == "q24":
            data = read_private_trace(path, preserve_crash_prefix=True)
        elif kind == "q27":
            data = read_trace(path, preserve_crash_prefix=True)
        else:
            raise ValueError("UNKNOWN_Q28_TRACE")
        return data, "COMMITTED_PREFIX_SCHEMA_VALID"
    except (Q24TraceRejected, Q27TraceRejected) as exc:
        return [], _safe_failure(exc)
    except OSError:
        return [], "PRIVATE_TRACE_FILE_UNAVAILABLE"


def _observed_class(ws: list[dict], guard: list[dict]) -> tuple[str, dict]:
    """Classify the first socket only, never infer a peer's intent."""
    first_close = next((
        e for e in ws
        if e["event"] == "SOCKET_CLOSE" and e["connection_index"] == 0
    ), None)
    # The original collector invokes ws.close() in its cleanup AFTER
    # receiving an unexpected close, even when the peer/transport went away
    # first. The first-close event sequence, not the eventual local-close
    # flag, establishes which event came first.
    close_seq = first_close["seq"] if first_close is not None else None
    errors = [
        e for e in ws
        if e["event"] == "SOCKET_ERROR"
        and e["connection_index"] == 0
        and (close_seq is None or e["seq"] < close_seq)
    ]
    local = any(
        e["event"] == "LOCAL_CLOSE_REQUEST"
        and e["connection_index"] == 0
        and (close_seq is None or e["seq"] < close_seq)
        for e in ws
    )
    cleanup_after_first_close = any(
        e["event"] == "LOCAL_CLOSE_REQUEST"
        and e["connection_index"] == 0
        and close_seq is not None and e["seq"] > close_seq
        for e in ws
    )
    timeouts = [
        e for e in guard if e["socket_index"] == 0 and e["event"] in (
            "FIRST_HEARTBEAT_TIMEOUT", "HEARTBEAT_STALE_FORCE_CLOSE",
        )
    ]
    reconnect = next((
        e for e in ws if e["event"] == "SOCKET_CONSTRUCTED"
        and e["connection_index"] == 1
    ), None)
    if timeouts and local and first_close:
        classification = "LOCAL_Q27_HEARTBEAT_GUARD_CLOSE_OBSERVED"
    elif timeouts and local:
        classification = "Q27_GUARD_CLOSE_REQUEST_WITHOUT_CLOSE_EVENT"
    elif timeouts:
        classification = "Q27_TIMEOUT_BUT_CLOSE_TRACE_UNVERIFIED"
    elif first_close and local:
        classification = "LOCAL_NON_GUARD_CLOSE_OBSERVED"
    elif first_close and errors:
        classification = "WEBSOCKET_ERROR_THEN_UNREQUESTED_CLOSE_OBSERVED"
    elif first_close and first_close["close_code"] == 1006:
        classification = "ABNORMAL_CLOSE_1006_WITHOUT_LOCAL_REQUEST"
    elif first_close:
        classification = "UNREQUESTED_CLOSE_OBSERVED"
    elif reconnect:
        classification = "RECONNECT_WITHOUT_FIRST_CLOSE_EVENT"
    else:
        classification = "INSUFFICIENT_TERMINATION_EVIDENCE"
    details = {
        "first_socket_close_observed": first_close is not None,
        "first_socket_close_code": (
            first_close["close_code"] if first_close else None
        ),
        "first_socket_clean_close": (
            first_close["was_clean"] if first_close else None
        ),
        "first_socket_local_close_request_observed": local,
        "first_socket_local_cleanup_after_close_observed":
            cleanup_after_first_close,
        "first_socket_error_events": len(errors),
        "first_socket_q27_timeout_events": len(timeouts),
        "first_socket_q27_timeout_class": (
            timeouts[0]["event"] if timeouts else None
        ),
        "first_socket_close_heartbeat_age_ms": (
            first_close["last_heartbeat_age_ms"] if first_close else None
        ),
        "first_socket_close_last_message_age_ms": (
            first_close["last_message_age_ms"] if first_close else None
        ),
        "second_socket_constructed": reconnect is not None,
        "observed_reconnect_delay_ms": (
            reconnect["reconnect_delay_ms"] if reconnect else None
        ),
    }
    return classification, details


def incident_receipt(
    q24_private: Path, q27_private: Path, *,
    collector_failed: bool = True
) -> dict:
    """This diagnostic can never authorize source release or real orders."""
    if type(collector_failed) is not bool:
        raise ValueError("INVALID_COLLECTOR_FAILURE_FLAG")
    ws, ws_valid = _read_one(q24_private, "q24")
    guard, guard_valid = _read_one(q27_private, "q27")
    incident_class, observed = _observed_class(ws, guard)
    ws_exited = bool(ws and ws[-1]["event"] == "PROCESS_EXIT")
    guard_exited = bool(guard and guard[-1]["event"] == "PROCESS_EXIT")
    return {
        "schema": "q28_public_termination_incident_v1",
        "classification": "DIAGNOSTIC_ONLY_NOT_SOURCE_ACCEPTANCE",
        "observed_termination_class": incident_class,
        "q24_trace_validation": ws_valid,
        "q27_trace_validation": guard_valid,
        "q24_committed_events": len(ws),
        "q27_committed_events": len(guard),
        "q24_process_exit_observed": ws_exited,
        "q27_process_exit_observed": guard_exited,
        "q24_trace_complete_for_normal_acceptance": (
            ws_valid == "COMMITTED_PREFIX_SCHEMA_VALID" and ws_exited
        ),
        "q27_trace_complete_for_normal_acceptance": (
            guard_valid == "COMMITTED_PREFIX_SCHEMA_VALID" and guard_exited
        ),
        "known_supervisor_failfast": collector_failed,
        "first_socket": observed,
        "guard_interventions_observed": (
            max(e["interventions_total"] for e in guard) if guard else None
        ),
        "guard_heartbeat_events_first_socket": next((
            e["heartbeat_count"] for e in reversed(guard)
            if e["socket_index"] == 0
        ), None),
        "source_continuity_certified": False,
        "network_root_cause_proven": False,
        "real_orders_enabled": False,
        "five_hour_certified": False,
        "persistent_24h_certified": False,
        "raw_payload_or_close_reason_published": False,
    }


def publish_incident(
    q24_private: Path, q27_private: Path, public: Path, *,
    collector_failed: bool = True
) -> dict:
    result = incident_receipt(
        q24_private, q27_private, collector_failed=collector_failed
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(
        os, "O_NOFOLLOW", 0
    )
    fd = os.open(public, flags, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return result
