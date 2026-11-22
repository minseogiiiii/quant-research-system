"""Q22 exact 5h private-source post-capture audit with two book interpreters.

Uses the frozen primary archive replay and separately implemented independent
soak replay, after a bounded large-archive integrity gate. All large limits
are process-local; NO source files or production defaults are changed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
def _load_after_backend_path_setup():
    from v36_3_durable_archive_gate import audit as durable_audit

    return durable_audit


durable_audit = _load_after_backend_path_setup()
del _load_after_backend_path_setup

SHA64 = re.compile(r"[a-f0-9]{64}\Z")


class FiveHourReplayRejected(ValueError):
    pass


def validate_observer_evidence(
    working: Path, evidence: dict, duration: int = 18000
) -> tuple[Path, str]:
    if (
        not isinstance(evidence, dict)
        or evidence.get("schema") != "q22_ci_observer_evidence_v1"
        or evidence.get("mode") != "LIVE_SOURCE"
        or evidence.get("ci_ephemeral_runner_only") is not True
        or evidence.get("durable_operator_mode") is not False
        or evidence.get("execution_authorized") is not False
        or evidence.get("trading_enabled") is not False
        or evidence.get("collector_exit_code") != 0
        or evidence.get("capture_ready_for_research") is not True
        or evidence.get("observer_reader_finished") is not True
        or evidence.get("observer_source_complete") is not True
        or evidence.get("capture_fault_count") != 0
        or not isinstance(evidence.get("elapsed_seconds"), (int, float))
        or type(evidence.get("elapsed_seconds")) is bool
        or evidence["elapsed_seconds"] < duration - 5
    ):
        raise FiveHourReplayRejected("CI_OBSERVER_SOURCE_NOT_FULLY_COMPLETED")
    health = evidence.get("observer_health_before_stop")
    if (
        not isinstance(health, dict)
        or health.get("mode") != "LIVE_SOURCE"
        or health.get("trading_enabled") is not False
        or health.get("connection_gaps") != 0
        or health.get("connection_id") != 0
        or set(health.get("snapshots", [])) != {"BTC-USD", "ETH-USD"}
    ):
        raise FiveHourReplayRejected("CI_OBSERVER_FINAL_SOURCE_INCOMPLETE")
    pin = evidence.get("captured_manifest_sha256")
    if not isinstance(pin, str) or not SHA64.fullmatch(pin):
        raise FiveHourReplayRejected("CAPTURE_OUTPUT_MANIFEST_SHA_MISSING")
    source = Path(evidence.get("captured_dir") or "")
    if (
        not source.is_dir()
        or source.is_symlink()
        or source.resolve().parent != (working / "soak_evidence").resolve()
    ):
        raise FiveHourReplayRejected("SOURCE_OUTSIDE_PRIVATE_CAPTURE_WORKDIR")
    return source, pin


def _book_agreement(primary: dict, independent: dict):
    if primary["messages"] != independent.get("messages"):
        raise FiveHourReplayRejected("PRIMARY_INDEPENDENT_MESSAGE_COUNT_MISMATCH")
    for product in ("BTC-USD", "ETH-USD"):
        main = primary["products"][product]
        check = independent["products"][product]
        if not 0 < main["retained_states"] <= 30_000:
            raise FiveHourReplayRejected("PRIMARY_NOT_MEMORY_BOUNDED")
        for key in ("best_bid", "best_ask"):
            try:
                consistent = Decimal(main[key]) == Decimal(check[key])
            except (InvalidOperation, TypeError, KeyError) as exc:
                raise FiveHourReplayRejected("BAD_FINAL_BOOK_" + product) from exc
            if not consistent:
                raise FiveHourReplayRejected("FINAL_BOOK_DIVERGENCE_" + product)
        if (
            main["bid_levels"] != check["bid_levels"]
            or main["ask_levels"] != check["ask_levels"]
        ):
            raise FiveHourReplayRejected("FINAL_BOOK_DEPTH_DIVERGENCE_" + product)


def _worker_run(source: Path, pin: str, mode: str) -> dict:
    # Replay prices are retained only in private files and never uploaded.
    receipt = source.parent.parent / ("q23_" + mode + "_private.json")
    if receipt.exists() or receipt.is_symlink():
        raise FiveHourReplayRejected("PRIVATE_WORKER_OUTPUT_ALREADY_EXISTS")
    try:
        process = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/q23_isolated_replay_worker.py"),
                "--mode", mode, "--capture", str(source),
                "--manifest-sha256", pin,
                "--private-output", str(receipt),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1100,
            check=False,
        )
        if process.returncode != 0 or not receipt.is_file():
            raise FiveHourReplayRejected("BOUNDED_" + mode.upper() + "_WORKER_FAILED")
        if not 0 < receipt.stat().st_size <= 64_000:
            raise FiveHourReplayRejected("PRIVATE_WORKER_RECEIPT_UNBOUNDED")
        result = json.loads(receipt.read_bytes())
        if (
            result.get("schema") != "q23_bounded_private_replay_worker_v1"
            or result.get("mode") != mode
            or result.get("max_as_bytes") != 3 * 1024**3
            or result.get("real_orders_enabled") is not False
            or result.get("rss_peak_bytes", 0) > 3 * 1024**3
        ):
            raise FiveHourReplayRejected("UNTRUSTED_BOUNDED_WORKER_RECEIPT")
        return result
    except subprocess.TimeoutExpired as exc:
        raise FiveHourReplayRejected("REPLAY_" + mode.upper() + "_TIMEOUT") from exc
    finally:
        receipt.unlink(missing_ok=True)


def audit(working: Path, duration: int = 18000) -> dict:
    working = Path(working)
    data = (working / "Q22_CI_OBSERVER_EVIDENCE.json").read_bytes()
    evidence = json.loads(data)
    source, pin = validate_observer_evidence(working, evidence, duration)
    manifest_bytes = (source / "manifest.json").read_bytes()
    if sha256(manifest_bytes).hexdigest() != pin:
        raise FiveHourReplayRejected("PIN_MISMATCH_BEFORE_INDEPENDENT_AUDIT")
    manifest = json.loads(manifest_bytes)
    if (
        manifest.get("required_duration_seconds") != duration
        or manifest.get("total_reconnects") != 0
        or manifest.get("finish_reason") != "DURATION_REACHED"
        or len(manifest.get("connections", [])) != 1
        or manifest.get("orders_enabled") is not False
        or manifest.get("synthetic_fixture") is True
        or manifest.get("controlled_fault_injection") is not None
    ):
        raise FiveHourReplayRejected("MANIFEST_SOURCE_NOT_CONTINUOUS_OR_NOT_REAL")
    gate = durable_audit(source, pin)
    if (
        gate["classification"] != "VERIFIED_CLEAN_ARCHIVE_ENVELOPE"
        or gate["eligible_for_archived_replay"] is not True
        or gate["source_connections"] != 1
        or gate["monotonic_source_span_seconds"] < duration - 5
    ):
        raise FiveHourReplayRejected("LARGE_SOURCE_ARCHIVE_GATE_NOT_ADMITTED")

    primary_receipt = _worker_run(source, pin, "primary")
    independent_receipt = _worker_run(source, pin, "independent")
    primary = primary_receipt["data"]
    independent = independent_receipt["data"]
    if (
        primary["messages"] != gate["sealed_messages_verified"]
        or primary["chunks"] != gate["sealed_chunks_verified"]
        or primary["connections"] != 1
        or independent["messages"] != gate["sealed_messages_verified"]
        or independent["chunks"] != gate["sealed_chunks_verified"]
        or independent["connections"] != 1
    ):
        raise FiveHourReplayRejected("FINAL_ARCHIVE_REPLAY_COUNT_DIVERGENCE")
    _book_agreement(primary, independent)
    if sha256((source / "manifest.json").read_bytes()).hexdigest() != pin:
        raise FiveHourReplayRejected("MANIFEST_MUTATED_DURING_REPLAY")
    return {
        "schema": "q22_5h_capture_two_replay_gate_v1",
        "status": "PASS",
        "mode": "REAL_COINBASE_PUBLIC_SOURCE_CAPTURE",
        "elapsed_observer_seconds": evidence["elapsed_seconds"],
        "source_monotonic_seconds": gate["monotonic_source_span_seconds"],
        "expected_source_seconds": duration,
        "source_connections": 1,
        "source_reconnects": 0,
        "messages": independent["messages"],
        "chunks": independent["chunks"],
        "archive_bytes": gate["sealed_bytes_verified"],
        "manifest_output_sha256": pin,
        "observer_evidence_sha256": sha256(data).hexdigest(),
        "primary_final_state_sha256": primary["final_state_sha256"],
        "bounded_replay_worker_max_as_bytes": 3 * 1024**3,
        "primary_replay_peak_rss_bytes": primary_receipt["rss_peak_bytes"],
        "independent_replay_peak_rss_bytes": independent_receipt["rss_peak_bytes"],
        "primary_retained_state_rows_per_product": {
            p: primary["products"][p]["retained_states"]
            for p in ("BTC-USD", "ETH-USD")
        },
        "independent_final_book_sha256": {
            p: independent["products"][p]["sha256"]
            for p in ("BTC-USD", "ETH-USD")
        },
        "primary_independent_final_books_agree": True,
        "source_attestation": "HOST_PID_AND_CLOCK_NOT_SIGNED_EXCHANGE",
        "source_manifest_pin_independent_pre_capture": False,
        "persistent_host_24h_certified": False,
        "oos_profitability_verified": False,
        "real_orders_enabled": False,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--private-workdir", type=Path, required=True)
    p.add_argument("--output-summary", type=Path, required=True)
    p.add_argument("--duration", type=int, choices=[180, 18000], default=18000)
    args = p.parse_args(argv)
    try:
        result = audit(args.private_workdir, args.duration)
        out = args.output_summary
        if out.exists() or out.is_symlink():
            raise FiveHourReplayRejected("REFUSE_OVERWRITE_AUDIT_SUMMARY")
        with out.open("x") as f:
            json.dump(result, f, sort_keys=True, indent=2)
            f.write("\n")
        print("PASS_Q22_FULL_PRIVATE_ARCHIVE_TWO_REPLAYS_NO_ORDERS "
              + json.dumps(result, sort_keys=True), flush=True)
    except (FiveHourReplayRejected, ValueError, TypeError, OSError, KeyError,
            IndexError, AssertionError) as exc:
        print("FAIL_Q22_ARCHIVE_OR_REPLAY " + type(exc).__name__ + ":"
              + str(exc).splitlines()[0][:180], flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
