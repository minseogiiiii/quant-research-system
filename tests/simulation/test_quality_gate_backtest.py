from world_quant_system.data import run_quality_gate_backtest


def test_deterministic_quality_gate_backtest() -> None:
    result = run_quality_gate_backtest(cases_per_bucket=500)

    assert result.total_cases == 6_000
    assert result.accuracy == 1.0
    assert result.unsafe_recall == 1.0
    assert result.false_quarantines == 0
    assert result.alpha_preserved == result.alpha_preservation_cases
