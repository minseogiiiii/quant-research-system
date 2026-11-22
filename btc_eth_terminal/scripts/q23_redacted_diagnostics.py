"""Q23 minimal privacy-preserving health fault receipts for long LIVE CI.

Only stable reason enums and bounded numeric counters leave the private host.
No market prices, raw frames, source session IDs, IPs, process output or paths.
An HTTP 503 from the Unix operator is parsed as fault evidence, NEVER health.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import time
from hashlib import sha256
from pathlib import Path

from q21_host_witness import checked_socket

BODY_LIMIT = 32768
RECENT_SAMPLES = 16
HEALTH_STATES = frozenset((
    "HEALTHY", "STALE", "RECONNECTING", "WAITING_FOR_SNAPSHOT",
    "QUARANTINED", "STOPPED",
))
REASON_ENUM = frozenset((
    "SOURCE_ENVELOPE_REJECTED",
    "SOURCE_REQUIRES_NEW_CONNECTION",
    "SOURCE_STOPPED",
    "NO_COLLECTOR_MESSAGES_WITHIN_STALENESS_BUDGET",
    "HEARTBEAT_STALE",
    "PRODUCT_L2_STALE_OR_CLOCK_ANOMALY",
    "COLLECTOR_REPORTED_WEBSOCKET_SOURCE_GAP",
    "LOCAL_OBSERVER_BACKPRESSURE_EXCEEDED",
    "OBSERVER_SOCKET_FAILURE",
    "COLLECTOR_OBSERVATION_SOCKET_CLOSED",
    "SOURCE_TRANSPORT_LOST",
    "LIVE_ADMISSION_REJECTED",
    "INVALID_OBSERVER_TRANSPORT_CONTROL",
    "COLLECTOR_PEER_CREDENTIAL_MISMATCH",
    "WAITING_FOR_DUAL_SNAPSHOT_AND_ACKS",
))
DIAGNOSTIC_CODES = frozenset((
    "OPERATOR_HEALTH_HTTP_503", "OPERATOR_METRICS_NON200",
    "PRIVATE_MONITOR_READ_FAILED", "HEALTH_MODE", "SOURCE_NOT_HEALTHY",
    "SOURCE_NOT_LIVE", "ORDERS_ENABLED", "SNAPSHOT_MISSING",
    "SUBSCRIPTION_MISSING", "SOURCE_CONNECTION_GAP", "CONNECTION_ID_CHANGED",
    "SESSION_ID_MISSING", "MESSAGE_OR_SEQUENCE_INVALID",
    "RECEIVER_TIME_INVALID", "RECEIVER_STALE", "HEARTBEAT_COUNTER_INVALID",
    "SSE_BOUND_EXCEEDED", "METRICS_UNTRUSTED", "METRICS_NOT_HEALTHY",
    "SOURCE_SESSION_CHANGED", "NO_MESSAGES_SINCE_LAST_SAMPLE",
    "HEARTBEAT_REGRESSION", "UNKNOWN_CHECK_FAILED",
))
PRODUCTS = frozenset(("BTC-USD", "ETH-USD"))
CHANNELS = frozenset(("heartbeats", "level2", "market_trades"))


class DiagnosticError(ValueError):
    pass


def _int(obj: dict, key: str, cap: int = 2**63 - 1) -> int | None:
    v = obj.get(key)
    return v if type(v) is int and 0 <= v <= cap else None


def _digest_session(obj: dict) -> str | None:
    s = obj.get("session_id")
    if isinstance(s, str) and re.fullmatch("[0-9a-f]{32}", s):
        return sha256(s.encode()).hexdigest()
    return None


def _enum_reason(health: dict) -> str:
    reason = health.get("reason")
    if not isinstance(reason, str):
        return "UNKNOWN"
    prefix = reason.split(":", 1)[0]
    return prefix if prefix in REASON_ENUM else "OTHER_REDACTED"


def _safe_set(obj: dict, name: str, allowed: frozenset[str]) -> bool:
    value = obj.get(name)
    return isinstance(value, list) and set(value) == allowed


def read_private_status(path: Path, route: str) -> tuple[int, dict]:
    """200 or 503 operator response; NEVER reinterpret 503 as a PASS."""
    if route not in ("/api/v36/health", "/api/v36/metrics"):
        raise DiagnosticError("UNREGISTERED_OPERATOR_ROUTE")
    checked_socket(path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(3)
        sock.connect(str(path))
        sock.sendall(
            ("GET " + route + " HTTP/1.1\r\n"
             "Host: localhost\r\nConnection: close\r\n\r\n").encode()
        )
        response = http.client.HTTPResponse(sock)
        response.begin()
        length = response.getheader("Content-Length")
        if (
            response.status not in (200, 503)
            or response.getheader("Transfer-Encoding")
            or not length or not length.isascii() or not length.isdecimal()
            or not 0 < int(length) <= BODY_LIMIT
        ):
            raise DiagnosticError("INVALID_PRIVATE_OPERATOR_HTTP")
        body = response.read(int(length) + 1)
        if len(body) != int(length):
            raise DiagnosticError("TRUNCATED_PRIVATE_OPERATOR_HTTP")
        value = json.loads(body)
        if not isinstance(value, dict):
            raise DiagnosticError("NON_OBJECT_OPERATOR_RESPONSE")
        return response.status, value


def named_failures(
    health: dict, metrics: dict, previous: dict | None = None,
    now_ms: int | None = None, health_http: int = 200,
    metrics_http: int = 200,
) -> list[str]:
    """The exact supervisor health gate represented as stable named checks."""
    now_ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    errors = []
    def add(flag, name):
        if flag:
            errors.append(name)

    add(health_http != 200, "OPERATOR_HEALTH_HTTP_503")
    add(metrics_http != 200, "OPERATOR_METRICS_NON200")
    add(health.get("mode") != "LIVE_SOURCE", "HEALTH_MODE")
    add(
        health.get("status") != "HEALTHY"
        or health.get("operationally_healthy") is not True,
        "SOURCE_NOT_HEALTHY",
    )
    add(health.get("live_feed") is not True, "SOURCE_NOT_LIVE")
    add(health.get("trading_enabled") is not False, "ORDERS_ENABLED")
    add(not _safe_set(health, "snapshots", PRODUCTS), "SNAPSHOT_MISSING")
    add(not _safe_set(health, "subscriptions", CHANNELS), "SUBSCRIPTION_MISSING")
    add(_int(health, "connection_gaps") != 0, "SOURCE_CONNECTION_GAP")
    add(_int(health, "connection_id") != 0, "CONNECTION_ID_CHANGED")
    session_hash = _digest_session(health)
    add(session_hash is None, "SESSION_ID_MISSING")
    msgs = _int(health, "messages")
    seq = _int(health, "last_sequence")
    add(msgs is None or msgs == 0 or seq != msgs - 1,
        "MESSAGE_OR_SEQUENCE_INVALID")
    heartbeats = _int(health, "heartbeats")
    add(heartbeats is None, "HEARTBEAT_COUNTER_INVALID")
    received = _int(health, "last_receiver_time_ms")
    add(received is None, "RECEIVER_TIME_INVALID")
    add(received is None or not 0 <= now_ms - received <= 15000,
        "RECEIVER_STALE")
    add(
        metrics.get("max_sse_clients") != 8
        or _int(metrics, "active_sse_connections", 8) is None,
        "SSE_BOUND_EXCEEDED",
    )
    add(
        metrics.get("monitor_transport") != "UNIX_SOCKET"
        or metrics.get("trading_enabled") is not False
        or metrics.get("execution_authorized") is not False,
        "METRICS_UNTRUSTED",
    )
    if previous:
        add(session_hash != previous.get("session_hash"),
            "SOURCE_SESSION_CHANGED")
        add(msgs is None or msgs <= previous.get("messages", -1),
            "NO_MESSAGES_SINCE_LAST_SAMPLE")
        add(heartbeats is None or heartbeats < previous.get("heartbeats", -1),
            "HEARTBEAT_REGRESSION")
    return sorted(set(errors))


def redacted_snapshot(
    health: dict, metrics: dict, codes: list[str], now_ms: int | None = None
) -> dict:
    now_ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    last = _int(health, "last_receiver_time_ms")
    status = health.get("status")
    return {
        "source_status": status if status in HEALTH_STATES else "UNKNOWN",
        "source_reason_class": _enum_reason(health),
        "source_session_sha256": _digest_session(health),
        "source_connection_id": _int(health, "connection_id"),
        "source_gap_count": _int(health, "connection_gaps"),
        "source_messages": _int(health, "messages"),
        "source_sequence": _int(health, "last_sequence"),
        "heartbeat_count": _int(health, "heartbeats"),
        "last_receiver_age_ms": now_ms - last if last is not None else None,
        "active_sse_connections": _int(metrics, "active_sse_connections", 8),
        "source_status_reason_codes": sorted(set(c for c in codes
                                                if c in DIAGNOSTIC_CODES)),
        "execution_authorized": False,
        "trading_enabled": False,
    }


def recent_monitor_ring(samples: list[dict]) -> list[dict]:
    result = []
    for sample in samples[-RECENT_SAMPLES:]:
        s = sample.get("source", {})
        result.append({
            "elapsed_sample_index": _int(sample, "index"),
            "session_sha256": s.get("session_hash") if (
                isinstance(s.get("session_hash"), str)
                and re.fullmatch("[0-9a-f]{64}", s["session_hash"])
            ) else None,
            "messages": _int(s, "messages"),
            "heartbeats": _int(s, "heartbeats"),
            "source_age_ms": _int(s, "staleness_ms", 60000),
            "sse_clients": _int(s, "active_sse", 8),
            "rss_bytes": _int(sample, "rss_total_bytes"),
            "fds": _int(sample, "fd_total"),
            "disk_free_bytes": _int(sample, "disk_free_bytes"),
        })
    return result


def failure_receipt(
    health: dict, metrics: dict, codes: list[str], samples: list[dict],
    elapsed: float, windows: int, http_status: int | None,
) -> dict:
    return {
        "schema": "q23_redacted_live_fault_v1",
        "classification": "FAIL_CLOSED_UNVERIFIED_MARKET_SOURCE",
        "elapsed_seconds": round(float(elapsed), 3),
        "passed_sse_windows": windows,
        "operator_health_http_status": http_status
        if http_status in (200, 503) else None,
        "last_private_status": redacted_snapshot(health, metrics, codes),
        "recent_samples": recent_monitor_ring(samples),
        "private_raw_frames_retained": False,
        "source_identity_revealed": False,
        "real_orders_enabled": False,
        "five_hour_certified": False,
    }
