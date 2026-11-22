"""Isolated, bounded-memory 5h archive replay worker.

One trusted engine per process. Original Q14 modules are untouched. The
explicit history bound increases from 10k to 30k for <=~8h at 1Hz, but stays
finite. OS RLIMIT_AS and RLIMIT_CPU fail closed. No model orders or network.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from independent_soak_replay import replay as independent_replay  # noqa: E402
from v36_3_durable_archive_gate import BOUNDS  # noqa: E402

PRIVATE_STATE_ROWS_PER_PRODUCT = 30_000
WORKER_AS_BYTES = 3 * 1024**3
WORKER_CPU_SECONDS = 1000


class BoundedReplayError(ValueError):
    pass


def apply_worker_limits() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (WORKER_AS_BYTES, WORKER_AS_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (
        WORKER_CPU_SECONDS, WORKER_CPU_SECONDS + 5
    ))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def verify_manifest_pin(source: Path, pin: str) -> None:
    if not source.is_dir() or source.is_symlink():
        raise BoundedReplayError("INVALID_PRIVATE_CAPTURE_SOURCE")
    if sha256((source / "manifest.json").read_bytes()).hexdigest() != pin:
        raise BoundedReplayError("PRE_WORKER_MANIFEST_PIN_MISMATCH")


def replay_primary(source: Path, pin: str) -> dict:
    import v35_archive_ingress as ingress
    import v35_replay_service as primary

    for key, value in BOUNDS.items():
        setattr(ingress, key, value)
    # Verification-only, fixed-size full five-hour state history.
    # Production source, v35 defaults, and client APIs stay unchanged.
    primary.MAX_ROWS_PER_PRODUCT = PRIVATE_STATE_ROWS_PER_PRODUCT
    dataset = primary.replay_capture(source, pin)
    products = {}
    for product in ("BTC-USD", "ETH-USD"):
        rows = dataset.rows[product]
        if len(rows) > PRIVATE_STATE_ROWS_PER_PRODUCT:
            raise BoundedReplayError("PRIMARY_STATE_HISTORY_NOT_BOUNDED")
        row = dataset.snapshot(product)["state"]
        if row["status"] != "VALID_ARCHIVED_QUOTE":
            raise BoundedReplayError("PRIMARY_FINAL_BOOK_NOT_VALID")
        products[product] = {
            "best_bid": row["best_bid"],
            "best_ask": row["best_ask"],
            "bid_levels": row["l2_book_levels"]["bid"],
            "ask_levels": row["l2_book_levels"]["offer"],
            "retained_states": len(rows),
        }
    return {
        "engine": "FROZEN_V35_PRIMARY_ISOLATED_30K_STATES",
        "messages": dataset.message_count,
        "chunks": dataset.certificate["chunks"],
        "connections": dataset.certificate["connections"],
        "final_state_sha256": dataset.final_state_sha256,
        "products": products,
    }


def replay_independent(source: Path, pin: str) -> dict:
    result = independent_replay(source)
    verify_manifest_pin(source, pin)
    return {
        "engine": "INDEPENDENT_DECIMAL_L2_REPLAY",
        "messages": result["messages"],
        "chunks": result["chunks"],
        "connections": result["connections"],
        "products": result["books_final"],
    }


def run(mode: str, source: Path, pin: str) -> dict:
    if mode not in ("primary", "independent"):
        raise BoundedReplayError("INVALID_REPLAY_ENGINE")
    apply_worker_limits()
    verify_manifest_pin(source, pin)
    result = (
        replay_primary(source, pin) if mode == "primary"
        else replay_independent(source, pin)
    )
    verify_manifest_pin(source, pin)
    return {
        "schema": "q23_bounded_private_replay_worker_v1",
        "mode": mode,
        "data": result,
        "max_as_bytes": WORKER_AS_BYTES,
        "cpu_seconds_limit": WORKER_CPU_SECONDS,
        "rss_peak_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        "real_orders_enabled": False,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=("primary", "independent"), required=True)
    ap.add_argument("--capture", type=Path, required=True)
    ap.add_argument("--manifest-sha256", required=True)
    ap.add_argument("--private-output", type=Path, required=True)
    a = ap.parse_args()
    try:
        result = run(a.mode, a.capture, a.manifest_sha256)
        if a.private_output.exists() or a.private_output.is_symlink():
            raise BoundedReplayError("REPLAY_RECEIPT_OVERWRITE_FORBIDDEN")
        fd = os.open(a.private_output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(result, out, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        return 0
    except Exception as exc:
        # Do not disclose raw wire data or any prices in runner logs.
        print("FAIL_Q23_BOUNDED_REPLAY " + a.mode + " " +
              type(exc).__name__, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
