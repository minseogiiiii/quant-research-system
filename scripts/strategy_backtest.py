#!/usr/bin/env python3
from world_quant_system.backtest.simulation import run_strategy_backtest_simulation


def main() -> None:
    result = run_strategy_backtest_simulation()
    buy = result.buy_and_hold.metrics
    sma = result.sma_crossover.metrics
    assert result.deterministic_digest_match
    assert result.look_ahead_violations == 0
    assert buy.commission_cost > 0
    assert buy.slippage_cost > 0
    assert result.buy_and_hold.replay_result.event_count == result.candle_count
    assert result.sma_crossover.replay_result.event_count == result.candle_count
    print(f"Synthetic strategy candles: {result.candle_count}")
    print(f"Buy-and-hold total return: {buy.total_return * 100:.4f}%")
    print(f"SMA crossover total return: {sma.total_return * 100:.4f}%")
    print(f"SMA closed trades: {sma.trade_count}")
    print(
        "Deterministic backtest digest: "
        f"{'match' if result.deterministic_digest_match else 'mismatch'}"
    )
    print(f"Look-ahead violations: {result.look_ahead_violations}")
    print("Transaction costs included: yes")
    print("Live trading and broker orders: disabled")


if __name__ == "__main__":
    main()
