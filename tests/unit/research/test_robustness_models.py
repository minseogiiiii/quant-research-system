from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from world_quant_system.research.models import ResearchWindow
from world_quant_system.research.robustness_models import (
    MarketRegime,
    PhaseResult,
    RobustnessCaseResult,
    RobustnessConfigurationError,
    RobustnessPhase,
    RobustnessPolicy,
    RobustnessReport,
    RobustnessStrategySpec,
    StressScenario,
    WalkForwardFold,
    WalkForwardPlan,
    classify_market_regime,
    sma_parameter_grid,
)

START = datetime(2024, 1, 1, tzinfo=UTC)
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def test_rolling_plan_is_deterministic_and_advances() -> None:
    plan = WalkForwardPlan.rolling(
        start=START,
        train_duration=timedelta(days=30),
        validation_duration=timedelta(days=10),
        test_duration=timedelta(days=10),
        step=timedelta(days=10),
        fold_count=3,
    )

    assert tuple(fold.fold_number for fold in plan.folds) == (1, 2, 3)
    assert plan.folds[1].train.start == START + timedelta(days=10)
    assert len(plan.fingerprint) == 64
    assert plan.fingerprint == WalkForwardPlan(plan.folds).fingerprint


def test_walk_forward_rejects_nonadvancing_test_windows() -> None:
    first = WalkForwardFold(
        1,
        ResearchWindow(START, START + timedelta(days=10)),
        ResearchWindow(START + timedelta(days=10), START + timedelta(days=20)),
        ResearchWindow(START + timedelta(days=20), START + timedelta(days=30)),
    )
    second = WalkForwardFold(
        2,
        ResearchWindow(START, START + timedelta(days=5)),
        ResearchWindow(START + timedelta(days=5), START + timedelta(days=15)),
        ResearchWindow(START + timedelta(days=15), START + timedelta(days=25)),
    )

    with pytest.raises(
        RobustnessConfigurationError,
        match="advance strictly",
    ):
        WalkForwardPlan((first, second))


def test_sma_parameter_grid_deduplicates_and_filters_invalid_values() -> None:
    grid = sma_parameter_grid(
        base_short_window=20,
        base_long_window=50,
        short_offsets=(0, 0, 40),
        long_offsets=(0, -40),
    )

    assert grid == (
        RobustnessStrategySpec(
            name="sma-crossover",
            short_window=20,
            long_window=50,
        ),
    )


def test_classify_market_regime_uses_benchmark_then_fallback() -> None:
    policy = RobustnessPolicy(high_volatility_threshold=0.20)

    assert (
        classify_market_regime(
            benchmark_return=Decimal("0.10"),
            fallback_return=Decimal("-0.50"),
            annualized_volatility=0.10,
            policy=policy,
        )
        is MarketRegime.UPTREND
    )
    assert (
        classify_market_regime(
            benchmark_return=None,
            fallback_return=Decimal("-0.10"),
            annualized_volatility=0.30,
            policy=policy,
        )
        is MarketRegime.HIGH_VOLATILITY_DOWNTREND
    )


def test_report_digest_ignores_creation_time_but_document_retains_it() -> None:
    policy = RobustnessPolicy(required_pass_rate=Decimal("0.50"))
    plan = WalkForwardPlan.rolling(
        start=START,
        train_duration=timedelta(days=30),
        validation_duration=timedelta(days=10),
        test_duration=timedelta(days=10),
        step=timedelta(days=10),
        fold_count=1,
    )
    case = _case(plan.folds[0], policy)

    first = RobustnessReport.build(
        created_at=START,
        dataset_digest=DIGEST_A,
        base_config_fingerprint=DIGEST_B,
        plan=plan,
        policy=policy,
        cases=(case,),
    )
    second = RobustnessReport.build(
        created_at=START + timedelta(days=1),
        dataset_digest=DIGEST_A,
        base_config_fingerprint=DIGEST_B,
        plan=plan,
        policy=policy,
        cases=(case,),
    )

    assert first.report_digest == second.report_digest
    assert first.report_id == second.report_id
    assert first.to_document()["created_at"] != second.to_document()["created_at"]
    assert first.pass_rate == Decimal("1")
    assert first.passed is True


def _case(
    fold: WalkForwardFold,
    policy: RobustnessPolicy,
) -> RobustnessCaseResult:
    strategy = RobustnessStrategySpec(
        name="sma-crossover",
        short_window=10,
        long_window=20,
    )
    scenario = StressScenario(
        cost_multiplier=Decimal("1"),
        execution_delay_candles=1,
    )
    train = _phase(
        RobustnessPhase.TRAIN,
        fold.train,
        Decimal("0.10"),
        DIGEST_A,
    )
    validation = _phase(
        RobustnessPhase.VALIDATION,
        fold.validation,
        Decimal("0.05"),
        DIGEST_B,
    )
    test = _phase(
        RobustnessPhase.TEST,
        fold.test,
        Decimal("0.04"),
        "c" * 64,
    )
    degradation = test.total_return - train.total_return
    reasons: tuple[str, ...] = ()
    assert degradation >= policy.maximum_return_degradation
    return RobustnessCaseResult.build(
        fold_number=fold.fold_number,
        strategy=strategy,
        scenario=scenario,
        train=train,
        validation=validation,
        test=test,
        regime=MarketRegime.SIDEWAYS,
        return_degradation=degradation,
        sharpe_degradation=None,
        passed=True,
        failure_reasons=reasons,
    )


def _phase(
    phase: RobustnessPhase,
    window: ResearchWindow,
    total_return: Decimal,
    digest: str,
) -> PhaseResult:
    return PhaseResult(
        phase=phase,
        window=window,
        event_count=30,
        run_digest=digest,
        total_return=total_return,
        maximum_drawdown=Decimal("-0.10"),
        sharpe_ratio=0.5,
        annualized_volatility=0.1,
        benchmark_return=Decimal("0.01"),
        commission_cost=Decimal("1"),
        slippage_cost=Decimal("1"),
    )
