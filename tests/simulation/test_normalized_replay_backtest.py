from world_quant_system.data import run_normalized_replay_backtest


def test_normalized_replay_backtest() -> None:
    result = run_normalized_replay_backtest()

    assert result.passed
    assert result.normalized_items == 2_000
    assert result.replayed_items == 2_000
    assert result.warning_items_retained == 500
