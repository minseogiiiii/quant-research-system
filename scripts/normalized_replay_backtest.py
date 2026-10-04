#!/usr/bin/env python3
from world_quant_system.data import run_normalized_replay_backtest


def main() -> None:
    result = run_normalized_replay_backtest()
    print(f"Normalized items: {result.normalized_items}")
    print(f"Replayed items: {result.replayed_items}")
    print(f"WARNING items retained: {result.warning_items_retained}")
    print(f"PASS-only items: {result.pass_only_items}")
    print(
        "Duplicate writes idempotent: "
        f"{'yes' if result.duplicate_writes_idempotent else 'no'}"
    )
    print(
        "Conflicting rewrite blocked: "
        f"{'yes' if result.conflicting_rewrite_blocked else 'no'}"
    )
    print(f"Quarantine blocked: {'yes' if result.quarantine_blocked else 'no'}")
    print(
        "Deterministic replay digest: "
        f"{'match' if result.deterministic_digest_match else 'mismatch'}"
    )
    if not result.passed:
        raise SystemExit("Normalized replay backtest failed.")


if __name__ == "__main__":
    main()
