#!/usr/bin/env python3
from world_quant_system.data.quality_backtest import run_quality_gate_backtest


def main() -> None:
    result = run_quality_gate_backtest()
    print(f"Quality-gate simulation cases: {result.total_cases}")
    print(f"Exact classification accuracy: {result.accuracy:.2%}")
    print(f"Unsafe-data quarantine recall: {result.unsafe_recall:.2%}")
    print(f"False quarantines: {result.false_quarantines}")
    print(
        "Alpha-preservation warnings retained: "
        f"{result.alpha_preserved}/{result.alpha_preservation_cases}"
    )
    if result.accuracy != 1.0:
        raise SystemExit("Quality-gate classification simulation failed.")
    if result.unsafe_recall != 1.0:
        raise SystemExit("Quality gate failed to block unsafe synthetic data.")
    if result.false_quarantines != 0:
        raise SystemExit("Quality gate produced a false quarantine.")
    if result.alpha_preserved != result.alpha_preservation_cases:
        raise SystemExit("Quality gate discarded synthetic alpha candidates.")


if __name__ == "__main__":
    main()
