"""v29 terminal run verdict for BTC/ETH public Coinbase source collection.

A successful collector exit is *not* a successful source admission. The caller
must first run the existing v26 independent source/clock crosscheck against
persisted evidence. This file checks the full contract, records no orders,
and never elevates self-reported clocks to independent exchange attestation.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from v25_capture_admission import parse_utc

MODE = {
    "smoke": (
        "real_public_smoke_60s",
        "HOST_MONITORED_INTERNAL_60S_CANDIDATE",
        "HOST_REPORTED_60S_AND_DUAL_REPLAY_CANDIDATE",
        3,
    ),
    "24h": (
        "real_public_24h",
        "HOST_MONITORED_INTERNAL_24H_CANDIDATE",
        "HOST_REPORTED_24H_AND_DUAL_REPLAY_CANDIDATE",
        250,
    ),
}


def sha(path):
    h = sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def evaluate(
    preflight,
    operator,
    crosscheck,
    *,
    ready_window,
    mode,
    workdir,
    evidence_dir,
    operator_exit_code,
    execution_requested,
):
    """Pure conservative verdict. Inputs must come from freshly inspected bytes."""
    if mode not in MODE:
        raise ValueError("INVALID_CAPTURE_MODE")
    run_mode, candidate_status, source_status, min_samples = MODE[mode]
    problems = []

    def need(condition, reason):
        if not condition:
            problems.append(reason)

    preflight = preflight if isinstance(preflight, dict) else {}
    operator = operator if isinstance(operator, dict) else {}
    crosscheck = crosscheck if isinstance(crosscheck, dict) else {}
    ready_window = ready_window if isinstance(ready_window, dict) else {}
    work = Path(workdir).resolve()
    ev = Path(evidence_dir).resolve()
    need(
        preflight.get("schema") == "v25_host_preflight_v1"
        and preflight.get("mode") == run_mode,
        "PREFLIGHT_MODE_OR_SCHEMA_MISMATCH",
    )
    need(
        preflight.get("ready_for_real_capture") is True
        and preflight.get("failures") == [],
        "PREFLIGHT_NOT_READY",
    )
    need(execution_requested is True, "CAPTURE_NOT_EXECUTED")
    need(
        type(operator_exit_code) is int and operator_exit_code == 0,
        "CAPTURE_OPERATOR_NONZERO_EXIT",
    )
    need(
        operator.get("schema") == "v25_operator_evidence_v1"
        and operator.get("run_mode") == run_mode,
        "OPERATOR_MODE_OR_SCHEMA_MISMATCH",
    )
    need(
        type(operator.get("capture_exit_code")) is int
        and operator["capture_exit_code"] == 0
        and operator.get("capture_failure") is None,
        "SOURCE_CAPTURE_FAILED",
    )
    need(
        operator.get("injection_options_disabled") is True
        and operator.get("orders_enabled") is False,
        "NO_ORDER_OR_INJECTION_CONTRACT_FAILED",
    )
    obs = preflight.get("observation")
    before = operator.get("before")
    if not isinstance(obs, dict) or not isinstance(before, dict):
        need(False, "MISSING_PREFLIGHT_OR_OPERATOR_HOST_OBSERVATION")
    else:
        need(
            obs.get("boot_id_digest") is not None
            and obs.get("boot_id_digest") == before.get("boot_id_digest"),
            "PREFLIGHT_BOOT_ID_UNBOUND",
        )
        need(
            obs.get("storage_root") == before.get("storage_root") == str(work),
            "PREFLIGHT_STORAGE_ROOT_UNBOUND",
        )
        try:
            prior_mono = obs["monotonic_ns"]
            before_mono = before["monotonic_ns"]
            if type(prior_mono) is not int or type(before_mono) is not int:
                raise ValueError("NONINTEGER_MONOTONIC")
            monotonic_delta = (before_mono - prior_mono) / 1e9
            wall_delta = (
                parse_utc(before["observed_utc"]) - parse_utc(obs["observed_utc"])
            ).total_seconds()
            need(
                0 <= monotonic_delta <= 30
                and 0 <= wall_delta <= 30
                and abs(monotonic_delta - wall_delta) <= 0.25,
                "STALE_OR_REVERSED_PREFLIGHT_OBSERVATION",
            )
        except (KeyError, ValueError, TypeError, OverflowError):
            need(False, "INVALID_PREFLIGHT_OBSERVATION_TIMING")
    source_str = operator.get("source_archive_dir")
    source = None
    if not isinstance(source_str, str) or not source_str:
        need(False, "NO_SOURCE_ARCHIVE_DIRECTORY")
    else:
        source = Path(source_str)
        expected_parent = work / "soak_evidence"
        need(
            source.is_absolute()
            and not source.is_symlink()
            and source.is_dir()
            and source.parent.resolve() == expected_parent.resolve()
            and source.name.startswith("coinbase_btc_eth_soak_"),
            "UNSAFE_OR_UNBOUND_SOURCE_DIRECTORY",
        )
        need(
            not source.resolve().is_relative_to(ev), "SOURCE_INSIDE_OBSERVER_DIRECTORY"
        )
    need(
        crosscheck.get("schema") == "v26_capture_crosscheck_v1"
        and crosscheck.get("status") == "HOST_MONITORED_INTERNAL_SOURCE_CANDIDATE"
        and crosscheck.get("candidate_host_monitored_source") is True,
        "V26_FULL_DUAL_REPLAY_AND_CLOCK_CROSSCHECK_NOT_ADMITTED",
    )
    prior = crosscheck.get("prior_internal_admission")
    need(
        isinstance(prior, dict)
        and prior.get("status") == source_status
        and prior.get("dual_replay_book_digests_match") is True,
        "WRONG_DURATION_OR_NO_INDEPENDENT_SOURCE_REPLAY",
    )
    trace = crosscheck.get("host_clock_trace")
    need(
        isinstance(trace, dict)
        and type(trace.get("samples")) is int
        and trace["samples"] >= min_samples
        and trace.get("host_reported_only") is True
        and trace.get("independent_exchange_time_attested") is False,
        "INCOMPLETE_OR_OVERCLAIMED_CLOCK_TRACE",
    )
    manifest_hash = operator.get("archive_manifest_sha256")
    need(
        isinstance(manifest_hash, str)
        and len(manifest_hash) == 64
        and crosscheck.get("source_manifest_sha256") == manifest_hash
        and isinstance(prior, dict)
        and prior.get("source_manifest_sha256") == manifest_hash,
        "MANIFEST_SOURCE_HASH_NOT_BOUND",
    )
    need(
        ready_window.get("schema") == "v29_source_ready_window_v1"
        and ready_window.get("source_window_contract_satisfied") is True
        and ready_window.get("mode") == mode
        and ready_window.get("required_ready_window_seconds")
        == (86400 if mode == "24h" else 60)
        and type(ready_window.get("observed_ready_window_seconds")) in (int, float)
        and ready_window["observed_ready_window_seconds"]
        >= (86400 if mode == "24h" else 60)
        and ready_window.get("host_reported_receiver_clock_not_exchange_attested")
        is True,
        "ACTUAL_BOTH_BOOKS_READY_WINDOW_NOT_PROVEN",
    )
    need(
        crosscheck.get("independent_exchange_truth_verified") is False
        and crosscheck.get("historical_feature_availability_verified") is False
        and crosscheck.get("executable_alpha_verified") is False
        and crosscheck.get("real_orders_sent") == 0,
        "UNSUPPORTED_MARKET_TRUTH_ALPHA_OR_ORDER_CLAIM",
    )
    return {
        "schema": "v29_public_capture_terminal_verdict_v1",
        "status": candidate_status if not problems else "BLOCKED_FINAL_CAPTURE_GATE",
        "capture_mode": run_mode,
        "execution_requested": execution_requested is True,
        "candidate_host_monitored_internal_source": not problems,
        "problems": sorted(set(problems)),
        "archive_manifest_sha256": manifest_hash
        if isinstance(manifest_hash, str)
        else None,
        "source_directory": str(source) if source else None,
        "full_crosscheck_required": True,
        "independent_exchange_time_attested": False,
        "historical_feature_label_pit_verified": False,
        "independent_blind_oos_eligible": False,
        "cost_aware_executable_alpha_confirmed": False,
        "real_orders_sent": 0,
    }
