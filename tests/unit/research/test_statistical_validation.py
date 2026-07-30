from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from world_quant_system.research.statistical_validation import (
    DeterministicStatisticalValidator,
)
from world_quant_system.research.statistical_validation_models import (
    FailedStatisticalTrial,
    StatisticalReturnsMatrix,
    StatisticalValidationPolicy,
)


def test_validator_is_deterministic_and_retains_failed_trials() -> None:
    matrix = _stable_matrix()
    policy = _permissive_policy()
    validator = DeterministicStatisticalValidator(
        matrix=matrix,
        dataset_digest="a" * 64,
        search_id="search-1",
        attempted_trial_count=5,
        failed_trials=(
            FailedStatisticalTrial(
                trial_id="failed",
                failure_reason="configuration rejected",
            ),
        ),
        policy=policy,
    )

    first = validator.run(created_at=datetime(2026, 1, 1, tzinfo=UTC))
    second = validator.run(created_at=datetime(2026, 2, 1, tzinfo=UTC))

    assert first.report_digest == second.report_digest
    assert first.report_id == second.report_id
    assert len(first.failed_trials) == 1
    assert first.omitted_trial_count == 0
    assert first.probability_backtest_overfitting.probability_backtest_overfitting == 0
    assert first.rank_stability.spearman_rank_correlation == 1.0


def test_deflated_sharpe_penalizes_more_attempted_trials() -> None:
    matrix = _stable_matrix()
    policy = _permissive_policy()

    small_search = DeterministicStatisticalValidator(
        matrix=matrix,
        dataset_digest="b" * 64,
        search_id="small",
        attempted_trial_count=4,
        policy=policy,
    ).run()
    large_search = DeterministicStatisticalValidator(
        matrix=matrix,
        dataset_digest="b" * 64,
        search_id="large",
        attempted_trial_count=100,
        effective_trial_count=100,
        policy=policy,
    ).run()

    assert (
        large_search.deflated_sharpe.expected_maximum_period_sharpe
        > small_search.deflated_sharpe.expected_maximum_period_sharpe
    )
    assert (
        large_search.deflated_sharpe.deflated_sharpe_probability
        <= small_search.deflated_sharpe.deflated_sharpe_probability
    )


def test_cscv_detects_regime_fitted_selection() -> None:
    report = DeterministicStatisticalValidator(
        matrix=_overfit_matrix(),
        dataset_digest="c" * 64,
        search_id="overfit",
        attempted_trial_count=4,
        policy=_permissive_policy(),
    ).run()

    assert (
        report.probability_backtest_overfitting.probability_backtest_overfitting
        > Decimal("0.80")
    )
    assert report.rank_stability.rank_reversal is True
    assert report.rank_stability.spearman_rank_correlation < 0


def _permissive_policy() -> StatisticalValidationPolicy:
    return StatisticalValidationPolicy(
        minimum_observations=80,
        cscv_partitions=8,
        minimum_deflated_sharpe_probability=Decimal("0.01"),
        maximum_probability_backtest_overfitting=Decimal("0.99"),
        minimum_rank_correlation=-1.0,
        require_bonferroni_pass=False,
        require_false_discovery_pass=False,
    )


def _stable_matrix() -> StatisticalReturnsMatrix:
    observation_count = 160
    start = datetime(2020, 1, 1, tzinfo=UTC)
    timestamps = tuple(
        start + timedelta(days=index) for index in range(observation_count)
    )
    series = tuple(
        tuple(
            Decimal(
                format(
                    mean + 0.0015 * math.sin(index * 0.3 + offset),
                    ".12f",
                )
            )
            for index in range(observation_count)
        )
        for offset, mean in enumerate((0.0015, 0.0010, 0.0005, 0.0001))
    )
    return StatisticalReturnsMatrix(
        timestamps=timestamps,
        trial_ids=("trial-a", "trial-b", "trial-c", "trial-d"),
        returns_by_trial=series,
    )


def _overfit_matrix() -> StatisticalReturnsMatrix:
    observation_count = 160
    start = datetime(2020, 1, 1, tzinfo=UTC)
    timestamps = tuple(
        start + timedelta(days=index) for index in range(observation_count)
    )
    series = []
    for trial_index in range(4):
        values = []
        for index in range(observation_count):
            block = index // 40
            mean = 0.003 if block == trial_index else -0.0005
            values.append(
                Decimal(
                    format(
                        mean + 0.001 * math.sin(index * 0.7 + trial_index),
                        ".12f",
                    )
                )
            )
        series.append(tuple(values))
    return StatisticalReturnsMatrix(
        timestamps=timestamps,
        trial_ids=("trial-a", "trial-b", "trial-c", "trial-d"),
        returns_by_trial=tuple(series),
    )
