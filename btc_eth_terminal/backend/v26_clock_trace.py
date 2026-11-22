"""v26 independent *host-reported* clock trace validator. No exchange attestation.

Samples are chained and fsynced by the operator. This detects accidental
truncation/mutation and time anomalies, but cannot defend against a compromised
host that forges the entire chain. A passing trace is NOT PIT/exchange truth.
"""

from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from v25_capture_admission import clock_pass, parse_utc, strict_object_pairs

MAX_SAMPLES = 600
MAX_LINE_BYTES = 24_000
ZERO = "0" * 64


class ClockTraceError(ValueError):
    pass


def canonical(obj):
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def record(sequence, previous_hash, observation):
    item = {
        "sequence": sequence,
        "prev_hash": previous_hash,
        "observation": observation,
    }
    item["hash"] = sha256(canonical(item)).hexdigest()
    return item


def inspect_trace(trace_file: Path, operator: dict, *, mode: str) -> dict:
    if mode not in ("real_public_smoke_60s", "real_public_24h"):
        raise ClockTraceError("INVALID_RUN_MODE")
    path = Path(trace_file)
    if path.is_symlink() or not path.is_file() or not path.stat().st_size:
        raise ClockTraceError("TRACE_MISSING_OR_SYMLINKED")
    if path.stat().st_size > MAX_SAMPLES * MAX_LINE_BYTES:
        raise ClockTraceError("TRACE_TOO_LARGE")
    max_gap = 35.0 if mode == "real_public_smoke_60s" else 330.0
    min_count = 3 if mode == "real_public_smoke_60s" else 250
    previous_hash, first_obs, last_obs = ZERO, None, None
    previous_utc: datetime | None = None
    previous_monotonic = None
    first_boot = first_storage = None
    counts = 0
    max_gap_seen = max_clock_drift = 0.0
    min_free_bytes = None
    with path.open("rb") as stream:
        for line in stream:
            counts += 1
            if counts > MAX_SAMPLES or not 0 < len(line) <= MAX_LINE_BYTES:
                raise ClockTraceError("TRACE_SAMPLE_OR_LINE_LIMIT")
            try:
                item = json.loads(
                    line,
                    object_pairs_hook=strict_object_pairs,
                    parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)),
                )
            except (ValueError, UnicodeError, TypeError) as exc:
                raise ClockTraceError("AMBIGUOUS_TRACE_JSON") from exc
            if not isinstance(item, dict) or set(item) != {
                "sequence",
                "prev_hash",
                "observation",
                "hash",
            }:
                raise ClockTraceError("UNEXPECTED_TRACE_SCHEMA")
            if type(item["sequence"]) is not int or item["sequence"] != counts - 1:
                raise ClockTraceError("TRACE_SEQUENCE_GAP")
            if item["prev_hash"] != previous_hash:
                raise ClockTraceError("TRACE_CHAIN_GAP")
            try:
                expected = sha256(
                    canonical(
                        {k: item[k] for k in ("sequence", "prev_hash", "observation")}
                    )
                ).hexdigest()
            except (ValueError, TypeError) as exc:
                raise ClockTraceError("NONCANONICAL_TRACE") from exc
            if item["hash"] != expected:
                raise ClockTraceError("TRACE_HASH_MISMATCH")
            previous_hash = expected
            obs = item["observation"]
            if not isinstance(obs, dict):
                raise ClockTraceError("INVALID_HOST_OBSERVATION")
            try:
                timestamp = parse_utc(obs.get("observed_utc"))
            except (ValueError, TypeError) as exc:
                raise ClockTraceError("INVALID_UTC_OBSERVATION") from exc
            mono = obs.get("monotonic_ns")
            boot = obs.get("boot_id_digest")
            storage = obs.get("storage_root")
            if type(mono) is not int or mono < 0:
                raise ClockTraceError("INVALID_MONOTONIC_CLOCK")
            if (
                not isinstance(boot, str)
                or len(boot) != 64
                or any(c not in "0123456789abcdef" for c in boot)
            ):
                raise ClockTraceError("INVALID_BOOT_ID_DIGEST")
            if not isinstance(storage, str) or not storage.startswith("/"):
                raise ClockTraceError("INVALID_STORAGE_ROOT")
            free = obs.get("free_storage_bytes")
            if type(free) is not int or free < 0:
                raise ClockTraceError("INVALID_INTERMEDIATE_FREE_STORAGE")
            min_free_bytes = (
                free if min_free_bytes is None else min(min_free_bytes, free)
            )
            if not clock_pass(obs.get("clock")):
                raise ClockTraceError("CHRONY_NOT_READY_AT_TRACE_SAMPLE")
            if counts == 1:
                first_obs, first_boot, first_storage = obs, boot, storage
            else:
                if boot != first_boot or storage != first_storage:
                    raise ClockTraceError("BOOT_OR_STORAGE_CHANGED")
                wall = (timestamp - previous_utc).total_seconds()
                monotonic = (mono - previous_monotonic) / 1e9
                if monotonic <= 0 or wall <= 0:
                    raise ClockTraceError("NONMONOTONIC_OBSERVATION")
                if monotonic > max_gap or wall > max_gap:
                    raise ClockTraceError("MISSING_INTERMEDIATE_HOST_OBSERVATION")
                max_gap_seen = max(max_gap_seen, monotonic)
                drift = abs(wall - monotonic)
                max_clock_drift = max(max_clock_drift, drift)
                if drift > 0.25:
                    raise ClockTraceError("INTERMEDIATE_WALL_MONOTONIC_DISAGREEMENT")
            previous_utc, previous_monotonic, last_obs = timestamp, mono, obs
    if counts < min_count:
        raise ClockTraceError("INSUFFICIENT_PERIODIC_OBSERVATIONS")
    if (
        not isinstance(operator, dict)
        or operator.get("before") != first_obs
        or operator.get("after") != last_obs
    ):
        raise ClockTraceError("OPERATOR_ENDPOINT_MISMATCH")
    return {
        "samples": counts,
        "last_trace_hash": previous_hash,
        "max_monotonic_gap_seconds": max_gap_seen,
        "max_wall_monotonic_drift_seconds": max_clock_drift,
        "min_free_storage_bytes": min_free_bytes,
        "first_observed_utc": first_obs["observed_utc"],
        "last_observed_utc": last_obs["observed_utc"],
        "host_reported_only": True,
        "independent_exchange_time_attested": False,
    }
