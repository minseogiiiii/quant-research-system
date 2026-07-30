from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from world_quant_system.backtest import BacktestConfig
from world_quant_system.backtest.simulation import (
    InMemoryNormalizedCandleReader,
    build_synthetic_records,
)
from world_quant_system.domain import CandleInterval
from world_quant_system.replay import ReplayConfig
from world_quant_system.research.robustness import (
    DeterministicRobustnessRunner,
    StandardBacktestRunFactory,
    build_stress_scenarios,
)
from world_quant_system.research.robustness_models import (
    RobustnessConfigurationError,
    RobustnessPolicy,
    WalkForwardPlan,
    sma_parameter_grid,
)

DATASET_DIGEST = "d" * 64
START = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_runner_is_deterministic_and_covers_full_grid() -> None:
    reader = InMemoryNormalizedCandleReader(build_synthetic_records(260))
    plan = WalkForwardPlan.rolling(
        start=START,
        train_duration=timedelta(days=80),
        validation_duration=timedelta(days=30),
        test_duration=timedelta(days=30),
        step=timedelta(days=60),
        fold_count=2,
    )
    strategies = sma_parameter_grid(
        base_short_window=10,
        base_long_window=30,
        short_offsets=(0, 2),
        long_offsets=(0,),
    )
    scenarios = build_stress_scenarios(
        cost_multipliers=(Decimal("1"), Decimal("2")),
        execution_delays=(1, 2),
    )
    config = _config()
    policy = RobustnessPolicy(
        minimum_observations=20,
        minimum_test_return=Decimal("-1"),
        maximum_drawdown=Decimal("-1"),
        maximum_return_degradation=Decimal("-2"),
        required_pass_rate=Decimal("0"),
    )

    first = await DeterministicRobustnessRunner(
        run_factory=StandardBacktestRunFactory(reader),
        base_config=config,
        plan=plan,
        strategies=strategies,
        scenarios=scenarios,
        policy=policy,
    ).run(created_at=datetime(2026, 1, 1, tzinfo=UTC))
    second = await DeterministicRobustnessRunner(
        run_factory=StandardBacktestRunFactory(reader),
        base_config=config,
        plan=plan,
        strategies=strategies,
        scenarios=scenarios,
        policy=policy,
    ).run(created_at=datetime(2026, 1, 2, tzinfo=UTC))

    assert len(first.cases) == 2 * 2 * 4
    assert first.report_digest == second.report_digest
    assert first.report_id == second.report_id
    assert {case.scenario.execution_delay_candles for case in first.cases} == {1, 2}
    assert {case.scenario.cost_multiplier for case in first.cases} == {
        Decimal("1"),
        Decimal("2"),
    }
    assert all(case.test.event_count == 30 for case in first.cases)


@pytest.mark.asyncio
async def test_runner_requires_frozen_historical_dataset_digest() -> None:
    config = BacktestConfig(
        replay=ReplayConfig(
            symbols=("005930",),
            interval=CandleInterval.DAY_1,
        ),
        initial_cash=Decimal("10000000"),
    )
    reader = InMemoryNormalizedCandleReader(build_synthetic_records(100))
    plan = WalkForwardPlan.rolling(
        start=START,
        train_duration=timedelta(days=30),
        validation_duration=timedelta(days=20),
        test_duration=timedelta(days=20),
        step=timedelta(days=10),
        fold_count=1,
    )

    with pytest.raises(
        RobustnessConfigurationError,
        match="frozen historical dataset digest",
    ):
        DeterministicRobustnessRunner(
            run_factory=StandardBacktestRunFactory(reader),
            base_config=config,
            plan=plan,
            strategies=sma_parameter_grid(
                base_short_window=5,
                base_long_window=10,
                short_offsets=(0,),
                long_offsets=(0,),
            ),
            scenarios=build_stress_scenarios(
                cost_multipliers=(Decimal("1"),),
                execution_delays=(1,),
            ),
            policy=RobustnessPolicy(),
        )


def _config() -> BacktestConfig:
    return BacktestConfig(
        replay=ReplayConfig(
            symbols=("005930",),
            interval=CandleInterval.DAY_1,
            page_size=23,
        ),
        initial_cash=Decimal("10000000"),
        commission_bps=Decimal("15"),
        slippage_bps=Decimal("10"),
        historical_dataset_digest=DATASET_DIGEST,
    )
