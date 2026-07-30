from world_quant_system.research.robustness_simulation import (
    run_robustness_simulation,
)


def test_robustness_validation_simulation() -> None:
    result = run_robustness_simulation()

    assert result.fold_count == 2
    assert result.strategy_count == 2
    assert result.scenario_count == 4
    assert result.case_count == 16
    assert result.deterministic_digest_match is True
