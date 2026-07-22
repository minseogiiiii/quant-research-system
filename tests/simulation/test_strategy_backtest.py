from world_quant_system.backtest.simulation import run_strategy_backtest_simulation


def test_strategy_backtest_simulation_is_deterministic_and_cost_aware() -> None:
    result = run_strategy_backtest_simulation()
    assert result.candle_count == 480
    assert result.deterministic_digest_match
    assert result.look_ahead_violations == 0
    assert result.buy_and_hold.metrics.commission_cost > 0
    assert result.buy_and_hold.metrics.slippage_cost > 0
    assert result.sma_crossover.metrics.trade_count >= 1
