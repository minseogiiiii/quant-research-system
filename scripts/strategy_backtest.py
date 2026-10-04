#!/usr/bin/env python3
from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

from world_quant_system.backtest.reporting import AtomicJsonBacktestSummaryWriter
from world_quant_system.backtest.simulation import run_strategy_backtest_simulation


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the deterministic synthetic strategy validation experiment."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Optionally write deterministic machine-readable backtest summaries.",
    )
    arguments = parser.parse_args()

    result = run_strategy_backtest_simulation()
    buy = result.buy_and_hold.metrics
    sma = result.sma_crossover.metrics
    assert result.deterministic_digest_match
    assert result.look_ahead_violations == 0
    assert buy.commission_cost > 0
    assert buy.slippage_cost > 0
    assert result.buy_and_hold.replay_result.event_count == result.candle_count
    assert result.sma_crossover.replay_result.event_count == result.candle_count

    if arguments.output_dir is not None:
        output_dir = arguments.output_dir
        AtomicJsonBacktestSummaryWriter(
            output_dir / "synthetic-buy-and-hold.json"
        ).write(result.buy_and_hold)
        AtomicJsonBacktestSummaryWriter(
            output_dir / "synthetic-sma-crossover.json"
        ).write(result.sma_crossover)

    print(f"Synthetic strategy candles: {result.candle_count}")
    print(f"Buy-and-hold total return: {buy.total_return * 100:.4f}%")
    print(f"SMA crossover total return: {sma.total_return * 100:.4f}%")
    print(f"SMA maximum drawdown: {sma.maximum_drawdown * 100:.4f}%")
    print(
        "SMA annualized volatility: "
        f"{_optional_percent(sma.annualized_volatility)}"
    )
    print(f"SMA Sharpe ratio: {_optional_number(sma.sharpe_ratio)}")
    print(f"SMA turnover: {sma.turnover:.6f}")
    print(
        "SMA benchmark return: "
        f"{_optional_decimal_percent(sma.benchmark_return)}"
    )
    print(f"SMA commission cost: {sma.commission_cost}")
    print(f"SMA slippage cost: {sma.slippage_cost}")
    print(f"SMA closed trades: {sma.trade_count}")
    print(
        "Deterministic backtest digest: "
        f"{'match' if result.deterministic_digest_match else 'mismatch'}"
    )
    print(f"Look-ahead violations: {result.look_ahead_violations}")
    print("Transaction costs included: yes")
    print("Live trading and broker orders: disabled")
    if arguments.output_dir is not None:
        print(f"Machine-readable summaries: {arguments.output_dir}")


def _optional_percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.4f}%"


def _optional_number(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.6f}"


def _optional_decimal_percent(value: Decimal | None) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100:.4f}%"


if __name__ == "__main__":
    main()
