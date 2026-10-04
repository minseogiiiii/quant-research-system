from __future__ import annotations

import math
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class StatisticalValidationSimulationResult:
    observation_count: int
    successful_trial_count: int
    failed_trial_count: int
    probability_backtest_overfitting: Decimal
    deflated_sharpe_probability: Decimal
    deterministic_digest_match: bool


def run_statistical_validation_simulation() -> StatisticalValidationSimulationResult:
    observation_count = 160
    start = datetime(2020, 1, 1, tzinfo=UTC)
    timestamps = tuple(
        start + timedelta(days=index) for index in range(observation_count)
    )
    stable = tuple(
        Decimal(format(0.0010 + 0.0015 * math.sin(index / 5), ".12f"))
        for index in range(observation_count)
    )
    weaker = tuple(
        Decimal(format(0.0004 + 0.0018 * math.cos(index / 7), ".12f"))
        for index in range(observation_count)
    )
    regime_fit = tuple(
        Decimal(
            format(
                (0.0022 if index < observation_count // 2 else -0.0014)
                + 0.0012 * math.sin(index / 3),
                ".12f",
            )
        )
        for index in range(observation_count)
    )
    noise = tuple(
        Decimal(format(0.0001 + 0.0020 * math.sin(index * 1.71), ".12f"))
        for index in range(observation_count)
    )
    matrix = StatisticalReturnsMatrix(
        timestamps=timestamps,
        trial_ids=("noise", "regime-fit", "stable", "weaker"),
        returns_by_trial=(noise, regime_fit, stable, weaker),
    )
    validator = DeterministicStatisticalValidator(
        matrix=matrix,
        dataset_digest="a" * 64,
        search_id="simulation-search",
        attempted_trial_count=5,
        effective_trial_count=5,
        failed_trials=(
            FailedStatisticalTrial(
                trial_id="failed-trial",
                failure_reason="intentional simulation failure",
            ),
        ),
        policy=StatisticalValidationPolicy(
            minimum_observations=80,
            cscv_partitions=8,
            minimum_deflated_sharpe_probability=Decimal("0.50"),
            maximum_probability_backtest_overfitting=Decimal("0.75"),
            minimum_rank_correlation=-1.0,
            require_bonferroni_pass=False,
            require_false_discovery_pass=False,
        ),
    )
    first = validator.run(created_at=datetime(2026, 1, 1, tzinfo=UTC))
    second = validator.run(created_at=datetime(2026, 2, 1, tzinfo=UTC))
    return StatisticalValidationSimulationResult(
        observation_count=observation_count,
        successful_trial_count=len(matrix.trial_ids),
        failed_trial_count=1,
        probability_backtest_overfitting=(
            first.probability_backtest_overfitting.probability_backtest_overfitting
        ),
        deflated_sharpe_probability=(
            first.deflated_sharpe.deflated_sharpe_probability
        ),
        deterministic_digest_match=(
            first.report_digest == second.report_digest
            and first.report_id == second.report_id
        ),
    )


def main() -> None:
    result = run_statistical_validation_simulation()
    print(f"Statistical observations: {result.observation_count}")
    print(f"Successful trials: {result.successful_trial_count}")
    print(f"Failed trials retained: {result.failed_trial_count}")
    print(
        "Deflated Sharpe probability: "
        f"{result.deflated_sharpe_probability:.4%}"
    )
    print(
        "Probability of backtest overfitting: "
        f"{result.probability_backtest_overfitting:.4%}"
    )
    print(
        "Deterministic statistical digest: "
        f"{'match' if result.deterministic_digest_match else 'mismatch'}"
    )
    print("Network access: disabled")
    print("Live trading and broker orders: disabled")


if __name__ == "__main__":
    main()
