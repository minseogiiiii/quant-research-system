"""Strict valid-ready market-source duration, not process wall time.

A Coinbase capture starts its useful window once both BTC-USD and ETH-USD
L2 snapshots have been received on the SAME WebSocket connection. A 24h source
must span at least 86,400 seconds from that instant to the connection's last
recorded message. This still does not independently attest the host clock.
"""

from __future__ import annotations

from v25_capture_admission import parse_utc

EXPECTED = {"smoke": 60, "24h": 86400}


class ReadyWindowError(ValueError):
    pass


def verify_ready_window(manifest, *, mode):
    if mode not in EXPECTED or not isinstance(manifest, dict):
        raise ReadyWindowError("INVALID_SOURCE_MODE")
    required = EXPECTED[mode]
    if manifest.get("producer_release") != "v20_multiday_bounded":
        raise ReadyWindowError("LEGACY_SOURCE_NOT_V29_READINESS_AWARE")
    if (
        type(manifest.get("required_duration_seconds")) is not int
        or manifest["required_duration_seconds"] != required
    ):
        raise ReadyWindowError("MANIFEST_REQUESTED_DURATION_NOT_MATCHED")
    if (
        manifest.get("status") != "CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH"
        or manifest.get("ready_for_research") is not True
        or manifest.get("synthetic_fixture") is not False
        or manifest.get("controlled_fault_injection") is not None
        or manifest.get("faults") != []
        or manifest.get("total_reconnects") != 0
    ):
        raise ReadyWindowError("SOURCE_NOT_CONTINUOUS_REAL_PUBLIC_CAPTURE")
    connections = manifest.get("connections")
    if (
        not isinstance(connections, list)
        or len(connections) != 1
        or not isinstance(connections[0], dict)
    ):
        raise ReadyWindowError("NOT_ONE_CONNECTION")
    connection = connections[0]
    if (
        connection.get("became_ready") is not True
        or connection.get("snapshots") != {"BTC-USD": True, "ETH-USD": True}
        or not isinstance(connection.get("updates"), dict)
        or any(
            type(connection["updates"].get(p)) is not int
            or connection["updates"][p] <= 0
            for p in ("BTC-USD", "ETH-USD")
        )
        or type(connection.get("heartbeat_count")) is not int
        or connection["heartbeat_count"] <= 0
        or connection.get("integrity_fault") is not False
    ):
        raise ReadyWindowError("BOTH_PRODUCTS_NOT_VALID_SNAPSHOT_AND_UPDATE_READY")
    start = connection.get("first_ready_utc")
    end = connection.get("last_received_utc")
    if not isinstance(start, str) or not isinstance(end, str):
        raise ReadyWindowError("MISSING_RECEIVER_READY_WINDOW")
    try:
        duration = (parse_utc(end) - parse_utc(start)).total_seconds()
        sampled = parse_utc(manifest.get("sampled_from_utc"))
        finished = parse_utc(manifest.get("finished_at_utc"))
    except (TypeError, ValueError) as exc:
        raise ReadyWindowError("INVALID_WINDOW_TIME") from exc
    if not (sampled <= parse_utc(start) <= parse_utc(end) <= finished):
        raise ReadyWindowError("SOURCE_TIME_BOUNDARY_ORDER_INVALID")
    if duration < required:
        raise ReadyWindowError(f"SOURCE_VALID_READY_WINDOW_UNDERRUN:{duration:.3f}s")
    if (finished - parse_utc(end)).total_seconds() > 12:
        raise ReadyWindowError("STALE_LAST_SOURCE_MESSAGE")
    if (
        manifest.get("capture_window_start_ready_utc") != start
        or manifest.get("capture_window_target_seconds") != required
    ):
        raise ReadyWindowError("READINESS_BASIS_NOT_DECLARED_IN_MANIFEST")
    return {
        "schema": "v29_source_ready_window_v1",
        "mode": mode,
        "connection_count": 1,
        "product_count": 2,
        "required_ready_window_seconds": required,
        "observed_ready_window_seconds": round(duration, 6),
        "first_both_books_ready_utc": start,
        "last_raw_message_received_utc": end,
        "process_duration_substituted_for_valid_source_time": False,
        "host_reported_receiver_clock_not_exchange_attested": True,
        "source_window_contract_satisfied": True,
        "independent_blind_oos_approved": False,
        "real_orders_sent": 0,
    }
