"""Q22 exact 5h private-source post-capture audit with two book interpreters.

Uses the frozen primary archive replay and separately implemented independent
soak replay, after a bounded large-archive integrity gate. All large limits
are process-local; NO source files or production defaults are changed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
def _load_after_backend_path_setup():
    from independent_soak_replay import replay as independent_replay
    from v36_3_durable_archive_gate import BOUNDS, audit as durable_audit

    return independent_replay, BOUNDS, durable_audit


independent_replay, BOUNDS, durable_audit = _load_after_backend_path_setup()
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


def _book_agreement(primary, independent: dict):
    if primary.message_count != independent.get("messages"):
        raise FiveHourReplayRejected("PRIMARY_INDEPENDENT_MESSAGE_COUNT_MISMATCH")
    for product in ("BTC-USD", "ETH-USD"):
        main = primary.snapshot(product)["state"]
        check = independent["books_final"][product]
        if main["status"] != "VALID_ARCHIVED_QUOTE":
            raise FiveHourReplayRejected("NO_FINAL_VALID_BOOK_" + product)
        for key in ("best_bid", "best_ask"):
            try:
                consistent = Decimal(main[key]) == Decimal(check[key])
            except (InvalidOperation, TypeError, KeyError) as exc:
                raise FiveHourReplayRejected("BAD_FINAL_BOOK_" + product) from exc
            if not consistent:
                raise FiveHourReplayRejected("FINAL_BOOK_DIVERGENCE_" + product)
        levels = main["l2_book_levels"]
        if (levels["bid"] != check["bid_levels"]
                or levels["offer"] != check["ask_levels"]):
            raise FiveHourReplayRejected("FINAL_BOOK_DEPTH_DIVERGENCE_" + product)


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

    # The pinned v35 original file is untouched. Follow the v36 durable gate
    # pattern of enlarging only the verifier's safety limits in this isolated
    # audit process after checking the externally captured SHA.
    import v35_archive_ingress as primary_ingress
    from v35_replay_service import replay_capture

    for key, value in BOUNDS.items():
        setattr(primary_ingress, key, value)
    primary = replay_capture(source, pin)
    independent = independent_replay(source)
    if (
        primary.message_count != gate["sealed_messages_verified"]
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
        "primary_final_state_sha256": primary.final_state_sha256,
        "independent_final_book_sha256": {
            p: independent["books_final"][p]["sha256"]
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
