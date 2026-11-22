"""Verify one freshly completed LIVE public capture without publishing its raw bytes.

Independent, previously pinned archived-source validation is a separate
research gate. Here the SHA is bound to the actual collector output and the
observer must match that entire immutable output, not only initial LIVE admission.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def _q14_load_after_path_setup_0():
    from independent_soak_replay import replay as independent_replay
    from v35_archive_ingress import verify_capture
    from v35_replay_service import StateRejected, replay_capture

    return independent_replay, verify_capture, StateRejected, replay_capture


independent_replay, verify_capture, StateRejected, replay_capture = (
    _q14_load_after_path_setup_0()
)
del _q14_load_after_path_setup_0


def verify_evidence(evidence_file: Path) -> dict:
    evidence = json.loads(Path(evidence_file).read_bytes())
    if (
        evidence.get("schema") != "v36_observer_evidence_v1"
        or evidence.get("mode") != "LIVE_SOURCE"
        or evidence.get("trading_enabled") is not False
        or evidence.get("capture_ready_for_research") is not True
        or evidence.get("observer_source_complete") is not True
        or evidence.get("collector_exit_code") != 0
    ):
        raise StateRejected("LIVE_OBSERVER_OR_COLLECTOR_INCOMPLETE")
    h = evidence.get("observer_health_before_stop")
    if not isinstance(h, dict) or h.get("live_feed") is not True:
        raise StateRejected("MISSING_REAL_OBSERVER_HEALTH")
    source = Path(evidence["captured_dir"])
    # Prevent accidental invocation on an arbitrary directory outside the
    # authorized observer's private workdir.
    if (
        not source.is_dir()
        or source.resolve().parent
        != Path(evidence_file).resolve().parent / "soak_evidence"
    ):
        raise StateRejected("SOURCE_NOT_IN_OBSERVER_WORKDIR")
    pin = evidence.get("captured_manifest_sha256")
    if not isinstance(pin, str) or len(pin) != 64:
        raise StateRejected("MISSING_CAPTURE_MANIFEST_DIGEST")
    manifest_bytes = (source / "manifest.json").read_bytes()
    if sha256(manifest_bytes).hexdigest() != pin:
        raise StateRejected("MANIFEST_CHANGED_AFTER_COLLECTOR_EXIT")
    ingress = verify_capture(source, pin)
    if (
        ingress["admission"] != "VERIFIED_ARCHIVE_ENVELOPE"
        or ingress["source_eligible_for_state_replay"] is not True
        or ingress["messages"] != h.get("messages")
    ):
        raise StateRejected("FINAL_ARCHIVE_NOT_ADMITTED_OR_TAP_MISMATCH")
    main = replay_capture(source, pin)
    independent = independent_replay(source)
    if (
        main.message_count != independent["messages"]
        or main.message_count != ingress["messages"]
    ):
        raise StateRejected("INDEPENDENT_REPLAY_MESSAGE_MISMATCH")
    books = {}
    for product in ("BTC-USD", "ETH-USD"):
        row = main.snapshot(product)["state"]
        check = independent["books_final"][product]
        if row["status"] != "VALID_ARCHIVED_QUOTE":
            raise StateRejected("NO_VALID_FINAL_ARCHIVED_QUOTE_" + product)
        for key in ("best_bid", "best_ask"):
            try:
                consistent = Decimal(row[key]) == Decimal(check[key])
            except (InvalidOperation, TypeError, KeyError):
                consistent = False
            if not consistent:
                raise StateRejected(
                    "INDEPENDENT_FINAL_" + key.upper() + "_MISMATCH_" + product
                )
        book_counts = row["l2_book_levels"]
        if (
            book_counts["bid"] != check["bid_levels"]
            or book_counts["offer"] != check["ask_levels"]
        ):
            raise StateRejected("INDEPENDENT_BOOK_DEPTH_COUNT_MISMATCH_" + product)
        books[product] = {
            "final_bid": row["best_bid"],
            "final_ask": row["best_ask"],
            "book_bid_levels": book_counts["bid"],
            "book_ask_levels": book_counts["offer"],
            "final_archived_receiver_time_ms": row["receiver_time_ms"],
        }
    return {
        "schema": "v36_real_capture_full_lifecycle_audit_v1",
        "evidence": "REAL_PUBLIC_CAPTURE_PRIVATE_RUNNER",
        "source_pin_kind": "CAPTURE_OUTPUT_SHA_NOT_EXTERNALLY_PREPINNED",
        "manifest_sha256": pin,
        "messages": ingress["messages"],
        "chunks": ingress["chunks"],
        "connection_count": ingress["connections"],
        "observer_all_messages_reconciled": True,
        "archive_ingress": "VERIFIED_ARCHIVE_ENVELOPE",
        "primary_replay_digest": main.final_state_sha256,
        "independent_replay": "PASS",
        "final_book_bid_ask_and_level_counts": books,
        "live_trading_enabled": False,
        "browser_live_e2e_verified": False,
        "24h_uninterrupted_verified": False,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--evidence", type=Path, required=True)
    args = p.parse_args()
    try:
        result = verify_evidence(args.evidence)
    except (ValueError, TypeError, KeyError, OSError, IndexError) as exc:
        print("FAIL_COINBASE_LIVE_POST_CAPTURE_AUDIT " + str(exc).splitlines()[0][:220])
        return 2
    # The output contains summary hashes and prices only, never raw JSONL.
    print("PASS_COINBASE_LIVE_POST_CAPTURE_AUDIT " + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
