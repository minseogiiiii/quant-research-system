from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

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
    RobustnessPolicy,
    WalkForwardPlan,
    sma_parameter_grid,
)

_DATASET_DIGEST = "a" * 64


@dataclass(frozen=True, slots=True)
class RobustnessSimulationResult:
    case_count: int
    fold_count: int
    strategy_count: int
    scenario_count: int
    deterministic_digest_match: bool
    report_passed: bool
    pass_rate: Decimal


def run_robustness_simulation() -> RobustnessSimulationResult:
    return asyncio.run(_run())


async def _run() -> RobustnessSimulationResult:
    records = build_synthetic_records(480)
    reader = InMemoryNormalizedCandleReader(records)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    plan = WalkForwardPlan.rolling(
        start=start,
        train_duration=timedelta(days=100),
        validation_duration=timedelta(days=40),
        test_duration=timedelta(days=40),
        step=timedelta(days=80),
        fold_count=2,
    )
    strategies = sma_parameter_grid(
        base_short_window=20,
        base_long_window=60,
        short_offsets=(0, 2),
        long_offsets=(0,),
    )
    scenarios = build_stress_scenarios(
        cost_multipliers=(Decimal("1"), Decimal("2")),
        execution_delays=(1, 2),
    )
    config = BacktestConfig(
        replay=ReplayConfig(
            symbols=("005930",),
            interval=CandleInterval.DAY_1,
            page_size=31,
        ),
        initial_cash=Decimal("10000000"),
        commission_bps=Decimal("15"),
        slippage_bps=Decimal("10"),
        max_volume_participation=Decimal("0.10"),
        historical_dataset_digest=_DATASET_DIGEST,
    )
    policy = RobustnessPolicy(
        minimum_observations=20,
        minimum_test_return=Decimal("-0.50"),
        maximum_drawdown=Decimal("-0.90"),
        maximum_return_degradation=Decimal("-1"),
        required_pass_rate=Decimal("0.50"),
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
    ).run(created_at=datetime(2026, 1, 1, tzinfo=UTC))
    return RobustnessSimulationResult(
        case_count=len(first.cases),
        fold_count=len(plan.folds),
        strategy_count=len(strategies),
        scenario_count=len(scenarios),
        deterministic_digest_match=(first.report_digest == second.report_digest),
        report_passed=first.passed,
        pass_rate=first.pass_rate,
    )


def main() -> None:
    result = run_robustness_simulation()
    print(f"Robustness cases: {result.case_count}")
    print(f"Walk-forward folds: {result.fold_count}")
    print(f"Strategy variants: {result.strategy_count}")
    print(f"Stress scenarios: {result.scenario_count}")
    print(
        "Deterministic robustness digest: "
        f"{'match' if result.deterministic_digest_match else 'mismatch'}"
    )
    print(f"Robustness pass rate: {result.pass_rate:.2%}")
    print(f"Robustness policy: {'PASS' if result.report_passed else 'FAIL'}")


if __name__ == "__main__":
    main()
