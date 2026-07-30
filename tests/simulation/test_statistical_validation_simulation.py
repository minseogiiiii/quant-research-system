from world_quant_system.research.statistical_validation_simulation import (
    run_statistical_validation_simulation,
)


def test_statistical_validation_simulation() -> None:
    result = run_statistical_validation_simulation()

    assert result.observation_count == 160
    assert result.successful_trial_count == 4
    assert result.failed_trial_count == 1
    assert result.deterministic_digest_match is True
