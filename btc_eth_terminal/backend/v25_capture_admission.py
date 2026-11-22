"""v25 fail-closed public-source admission overlay; zero trading interfaces.

Claims are limited to internal source replay and host-*reported* clock state.
A host clock observation is not an independent exchange timestamp attestation.
No promotion to alpha research, account execution, or actual maker fills.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from audit_soak import audit as primary_replay
from independent_soak_replay import replay as secondary_replay


class V25AdmissionError(ValueError):
    pass


def strict_object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise V25AdmissionError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def load_json(path: Path, max_bytes=3_000_000):
    if (
        path.is_symlink()
        or not path.is_file()
        or not 0 < path.stat().st_size <= max_bytes
    ):
        raise V25AdmissionError("UNSAFE_OR_OVERSIZED_EVIDENCE")
    try:
        return json.loads(path.read_bytes(), object_pairs_hook=strict_object_pairs)
    except (ValueError, UnicodeError) as error:
        raise V25AdmissionError("INVALID_EVIDENCE_JSON") from error


def parse_utc(s):
    if not isinstance(s, str) or not s.endswith("Z"):
        raise V25AdmissionError("UTC_Z_REQUIRED")
    try:
        x = datetime.fromisoformat(s[:-1] + "+00:00")
    except ValueError as error:
        raise V25AdmissionError("MALFORMED_UTC") from error
    if x.utcoffset().total_seconds() != 0:
        raise V25AdmissionError("NON_UTC")
    return x


def clock_pass(obs):
    if not isinstance(obs, dict):
        return False
    return (
        obs.get("method") == "chronyc_tracking_host_reported"
        and obs.get("leap_status") == "Normal"
        and type(obs.get("stratum")) is int
        and 1 <= obs["stratum"] <= 15
        and type(obs.get("system_offset_seconds")) in (int, float)
        and abs(obs["system_offset_seconds"]) <= 0.1
        and obs.get("command_exit_code") == 0
    )


def evaluate_operator_observation(observation, manifest, manifest_sha):
    (
        'Evaluate binding; caller mus'
        't *never* treat host self-re'
        'port as exchange truth.'
    )
    flags = []
    if (
        not isinstance(observation, dict)
        or observation.get("schema") != "v25_operator_evidence_v1"
    ):
        return ["MISSING_OR_INCOMPATIBLE_OPERATOR_EVIDENCE"]
    if observation.get("archive_manifest_sha256") != manifest_sha:
        flags.append("MANIFEST_BINDING_HASH_MISMATCH")
    if observation.get("run_mode") not in ("real_public_smoke_60s", "real_public_24h"):
        flags.append("INVALID_RUN_MODE")
    if (
        observation.get("run_mode") == "real_public_smoke_60s"
        and manifest.get("required_duration_seconds") != 60
    ):
        flags.append("SMOKE_DURATION_MISMATCH")
    if (
        observation.get("run_mode") == "real_public_24h"
        and manifest.get("required_duration_seconds") != 86400
    ):
        flags.append("FULL_DAY_DURATION_MISMATCH")
    if observation.get("capture_exit_code") != 0:
        flags.append("CAPTURE_EXIT_NONZERO")
    if observation.get("injection_options_disabled") is not True:
        flags.append("SYNTHETIC_INJECTION_NOT_PROVEN_DISABLED")
    if (
        observation.get("producer_script_sha256")
        != sha256(
            (
                Path(__file__).resolve().parents[1]
                / "scripts"
                / "btc_eth_multiday_v20.mjs"
            ).read_bytes()
        ).hexdigest()
    ):
        flags.append("PRODUCER_SCRIPT_HASH_MISMATCH")
    first, last = observation.get("before"), observation.get("after")
    if not isinstance(first, dict) or not isinstance(last, dict):
        return flags + ["MISSING_HOST_SNAPSHOTS"]
    if not isinstance(first.get("boot_id_digest"), str) or first.get(
        "boot_id_digest"
    ) != last.get("boot_id_digest"):
        flags.append("BOOT_ID_MISMATCH_OR_UNKNOWN")
    if not isinstance(first.get("storage_root"), str) or first.get(
        "storage_root"
    ) != last.get("storage_root"):
        flags.append("STORAGE_ROOT_CHANGED")
    if (
        not isinstance(first.get("dns_status"), str)
        or first.get("dns_status") != "RESOLVED_NO_WEBSOCKET_ATTESTATION"
    ):
        flags.append("INITIAL_DNS_NOT_VERIFIED")
    try:
        before_time = parse_utc(first.get("observed_utc"))
        after_time = parse_utc(last.get("observed_utc"))
        wall_seconds = (after_time - before_time).total_seconds()
        bmon = first.get("monotonic_ns")
        amon = last.get("monotonic_ns")
        if type(bmon) is not int or type(amon) is not int or bmon >= amon:
            raise V25AdmissionError("BAD_MONOTONIC")
        mono_seconds = (amon - bmon) / 1e9
        if abs(wall_seconds - mono_seconds) > 2.0:
            flags.append("HOST_CLOCK_JUMP_OVER_2S")
        if mono_seconds < manifest["required_duration_seconds"] - 3:
            flags.append("MONOTONIC_CAPTURE_UNDERRUN")
        if before_time > parse_utc(
            manifest["sampled_from_utc"]
        ) or after_time < parse_utc(manifest["finished_at_utc"]):
            flags.append("HOST_OBSERVATIONS_DO_NOT_BRACKET_SOURCE")
    except (V25AdmissionError, KeyError, TypeError) as error:
        flags.append("BAD_HOST_TIMING:" + str(error)[:40])
    if not clock_pass(first.get("clock")) or not clock_pass(last.get("clock")):
        flags.append("HOST_REPORTED_CHRONY_UNVERIFIED")
    # Explicitly host-reported and forgeable, even when every check passes.
    return flags


def assess_archive(source: Path, operator_evidence: Path | None = None):
    """No source bytes are modified. Reject on any failed independent replay."""
    source = Path(source)
    if source.is_symlink() or not source.is_dir():
        raise V25AdmissionError("UNSAFE_OR_MISSING_ARCHIVE_ROOT")
    manifest_path = source / "manifest.json"
    manifest = load_json(manifest_path)
    before_hash = sha256(manifest_path.read_bytes()).hexdigest()
    first = primary_replay(source)
    second = secondary_replay(source)
    if sha256(manifest_path.read_bytes()).hexdigest() != before_hash:
        raise V25AdmissionError("MANIFEST_MUTATED_DURING_REPLAY")
    if first.get("message_count") != second.get("messages") or first.get(
        "chunk_count"
    ) != second.get("chunks"):
        raise V25AdmissionError("INDEPENDENT_COUNT_DISAGREEMENT")
    for product in ("BTC-USD", "ETH-USD"):
        if (
            first["books_final"][product]["sha256"]
            != second["books_final"][product]["sha256"]
        ):
            raise V25AdmissionError("INDEPENDENT_BOOK_DISAGREEMENT")
    if first.get("status") != "INTERNALLY_REPLAYED_NO_KNOWN_GAP":
        raise V25AdmissionError("SOURCE_NOT_ACCEPTED_BY_PRIMARY_REPLAY")
    if (
        manifest.get("status") != "CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH"
        or manifest.get("ready_for_research") is not True
        or manifest.get("synthetic_fixture", False)
        or manifest.get("controlled_fault_injection") is not None
        or len(manifest.get("connections", [])) != 1
        or manifest.get("faults")
    ):
        raise V25AdmissionError("ARCHIVE_NOT_SINGLE_CONNECTION_SOURCE")
    # Older real archives can be re-audited, but do not acquire a v25 host status.
    modern = manifest.get("producer_release") == "v20_multiday_bounded"
    errors = ["LEGACY_PRODUCER_NOT_V25_RUN_ELIGIBLE"] if not modern else []
    if operator_evidence:
        observation = load_json(Path(operator_evidence))
        errors.extend(evaluate_operator_observation(observation, manifest, before_hash))
    else:
        errors.append("NO_OPERATOR_EVIDENCE")
    elapsed = (
        parse_utc(manifest["finished_at_utc"]) - parse_utc(manifest["sampled_from_utc"])
    ).total_seconds()
    day = bool(
        modern
        and manifest.get("required_duration_seconds") == 86400
        and elapsed >= 86400
        and not errors
    )
    smoke = bool(
        modern
        and manifest.get("required_duration_seconds") == 60
        and elapsed >= 58
        and not errors
    )
    status = (
        "HOST_REPORTED_24H_AND_DUAL_REPLAY_CANDIDATE"
        if day
        else "HOST_REPORTED_60S_AND_DUAL_REPLAY_CANDIDATE"
        if smoke
        else "INTERNAL_REPLAY_ONLY_HOST_EVIDENCE_INCOMPLETE"
    )
    return {
        "schema": "v25_capture_admission_v1",
        "status": status,
        "candidate_24h_internal_source": day,
        "candidate_real_60s_smoke": smoke,
        "operator_evidence_conditions_met": not errors,
        "operator_evidence_failures": errors,
        "source_manifest_sha256": before_hash,
        "source_producer_release": manifest.get("producer_release"),
        "duration_seconds_from_manifest_wall": elapsed,
        "messages": first["message_count"],
        "chunks": first["chunk_count"],
        "dual_replay_book_digests_match": True,
        "host_reported_chrony_only_not_independent_attestation": True,
        "independent_exchange_truth_verified": False,
        "historical_feature_availability_verified": False,
        "actual_execution_fills_verified": False,
        "executable_alpha_verified": False,
        "live_orders_sent": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--operator-evidence", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = assess_archive(args.archive, args.operator_evidence)
        status = result["status"]
    except (ValueError, KeyError, OSError, TypeError, OverflowError) as error:
        status = "REJECTED_SOURCE"
        result = {
            "schema": "v25_capture_admission_v1",
            "status": status,
            "reason": str(error)[:500],
            "candidate_24h_internal_source": False,
            "candidate_real_60s_smoke": False,
            "independent_exchange_truth_verified": False,
            "executable_alpha_verified": False,
            "live_orders_sent": False,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists() or args.output.is_symlink():
        raise SystemExit("Refusing to overwrite existing evidence output")
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(status)
    if status not in (
        "HOST_REPORTED_60S_AND_DUAL_REPLAY_CANDIDATE",
        "HOST_REPORTED_24H_AND_DUAL_REPLAY_CANDIDATE",
    ):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
