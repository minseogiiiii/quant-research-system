"""Bind observed, completed candles to one exact source row at publication."""

from copy import deepcopy


def candle_bundle(row, candles):
    rows = list(candles)
    cutoff = row["receiver_time_ms"]
    if len(rows) > 120 or any(
        c["available_at"] > cutoff or c["available_at"] < c["time"] + 60000
        for c in rows
    ):
        raise ValueError("F3_CANDLE_AVAILABILITY")
    return {
        "schema": "f3_source_completed_candles_v1",
        "product": row["product"],
        "mode": row["mode"],
        "run_id": row["run_id"],
        "session_id": row["session_id"],
        "connection_id": row["connection_id"],
        "state_version": row["state_version"],
        "source_sequence": row["source_sequence"],
        "source_raw_sha256": row["source_raw_sha256"],
        "asof_receiver_ms": cutoff,
        "incomplete_candle_never_published": True,
        "independent_candle_replay_verified": False,
        "execution_authorized": False,
        "rows": deepcopy(rows),
    }
