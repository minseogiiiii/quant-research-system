from __future__ import annotations

from collections import deque
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from world_quant_system.backtest import (
    BacktestConfig,
    BacktestRunResult,
    BuyAndHoldStrategy,
    SmaCrossoverStrategy,
    Strategy,
    StrategyBacktestEngine,
)
from world_quant_system.backtest.models import (
    PortfolioSnapshot,
    StrategyDescriptor,
    TargetPositionSignal,
)
from world_quant_system.data import NormalizedMarketDataReader
from world_quant_system.replay import ReplayConfig, ReplayEvent
from world_quant_system.research.models import ResearchWindow
from world_quant_system.research.robustness_models import (
    PhaseResult,
    RobustnessCaseResult,
    RobustnessConfigurationError,
    RobustnessPhase,
    RobustnessPolicy,
    RobustnessReport,
    RobustnessStrategySpec,
    StressScenario,
    WalkForwardPlan,
    classify_market_regime,
)

_ZERO = Decimal("0")


class BacktestRunFactory(Protocol):
    async def run(
        self,
        config: BacktestConfig,
        strategy: Strategy,
    ) -> BacktestRunResult:
        """Run one isolated deterministic backtest."""
        ...


class StandardBacktestRunFactory:
    """Networkless factory backed by one reusable normalized-data reader."""

    def __init__(self, reader: NormalizedMarketDataReader) -> None:
        self._reader = reader

    async def run(
        self,
        config: BacktestConfig,
        strategy: Strategy,
    ) -> BacktestRunResult:
        return await StrategyBacktestEngine(self._reader, config, strategy).run()


class DelayedTargetStrategy:
    """Delay observed target changes without exposing future candles."""

    def __init__(self, strategy: Strategy, *, execution_delay_candles: int) -> None:
        if (
            isinstance(execution_delay_candles, bool)
            or not isinstance(execution_delay_candles, int)
            or execution_delay_candles <= 0
        ):
            raise RobustnessConfigurationError(
                "Execution delay must be a positive candle count."
            )
        self._strategy = strategy
        self._delay = execution_delay_candles
        self._targets: deque[tuple[Decimal, str]] = deque()
        self._descriptor = StrategyDescriptor(
            name=f"delayed-{strategy.descriptor.name}",
            version="1.0.0",
            parameters=tuple(
                sorted(
                    (
                        *strategy.descriptor.parameters,
                        ("base_fingerprint", strategy.descriptor.fingerprint),
                        ("execution_delay_candles", str(execution_delay_candles)),
                    )
                )
            ),
        )

    def reset(self) -> None:
        self._strategy.reset()
        self._targets.clear()

    @property
    def descriptor(self) -> StrategyDescriptor:
        return self._descriptor

    def on_candle(
        self,
        event: ReplayEvent,
        portfolio: PortfolioSnapshot,
    ) -> TargetPositionSignal:
        observed = self._strategy.on_candle(event, portfolio)
        self._targets.append((observed.target_fraction, observed.reason))
        if len(self._targets) < self._delay:
            target = _ZERO
            reason = (
                f"Execution-delay warm-up {len(self._targets)}/{self._delay}; "
                "remain flat."
            )
        else:
            target, base_reason = self._targets.popleft()
            reason = (
                f"Delayed by {self._delay} candle(s): {base_reason}"
            )
        return TargetPositionSignal(
            signal_id=observed.signal_id,
            symbol=observed.symbol,
            generated_at=observed.generated_at,
            target_fraction=target,
            reason=reason,
        )


class DeterministicRobustnessRunner:
    """Run parameter, cost, delay, and walk-forward cases in stable order."""

    def __init__(
        self,
        *,
        run_factory: BacktestRunFactory,
        base_config: BacktestConfig,
        plan: WalkForwardPlan,
        strategies: tuple[RobustnessStrategySpec, ...],
        scenarios: tuple[StressScenario, ...],
        policy: RobustnessPolicy,
    ) -> None:
        dataset_digest = base_config.historical_dataset_digest
        if dataset_digest is None:
            raise RobustnessConfigurationError(
                "Robustness validation requires a frozen historical dataset digest."
            )
        if not strategies:
            raise RobustnessConfigurationError(
                "Robustness validation requires at least one strategy."
            )
        if not scenarios:
            raise RobustnessConfigurationError(
                "Robustness validation requires at least one stress scenario."
            )
        self._run_factory = run_factory
        self._base_config = base_config
        self._dataset_digest = dataset_digest
        self._plan = plan
        self._strategies = tuple(
            sorted(set(strategies), key=lambda item: item.fingerprint)
        )
        self._scenarios = tuple(
            sorted(set(scenarios), key=lambda item: item.fingerprint)
        )
        self._policy = policy
        self._has_run = False

    async def run(self, *, created_at: datetime | None = None) -> RobustnessReport:
        if self._has_run:
            raise RobustnessConfigurationError(
                "Robustness runner instances are single-use."
            )
        self._has_run = True
        cases: list[RobustnessCaseResult] = []
        for fold in self._plan.folds:
            for strategy_spec in self._strategies:
                for scenario in self._scenarios:
                    phases: dict[RobustnessPhase, PhaseResult] = {}
                    for phase in RobustnessPhase:
                        window = fold.window(phase)
                        config = self._config_for_window(
                            window_start=window.start,
                            window_end=window.end,
                            scenario=scenario,
                        )
                        strategy = DelayedTargetStrategy(
                            _build_strategy(strategy_spec),
                            execution_delay_candles=(
                                scenario.execution_delay_candles
                            ),
                        )
                        result = await self._run_factory.run(config, strategy)
                        phases[phase] = _phase_result(phase, window, result)
                    train = phases[RobustnessPhase.TRAIN]
                    validation = phases[RobustnessPhase.VALIDATION]
                    test = phases[RobustnessPhase.TEST]
                    return_degradation = test.total_return - train.total_return
                    sharpe_degradation = _optional_difference(
                        test.sharpe_ratio,
                        train.sharpe_ratio,
                    )
                    regime = classify_market_regime(
                        benchmark_return=test.benchmark_return,
                        fallback_return=test.total_return,
                        annualized_volatility=test.annualized_volatility,
                        policy=self._policy,
                    )
                    failure_reasons = _failure_reasons(
                        train=train,
                        validation=validation,
                        test=test,
                        return_degradation=return_degradation,
                        policy=self._policy,
                    )
                    cases.append(
                        RobustnessCaseResult.build(
                            fold_number=fold.fold_number,
                            strategy=strategy_spec,
                            scenario=scenario,
                            train=train,
                            validation=validation,
                            test=test,
                            regime=regime,
                            return_degradation=return_degradation,
                            sharpe_degradation=sharpe_degradation,
                            passed=not failure_reasons,
                            failure_reasons=failure_reasons,
                        )
                    )
        timestamp = created_at or datetime.now(UTC)
        return RobustnessReport.build(
            created_at=timestamp,
            dataset_digest=self._dataset_digest,
            base_config_fingerprint=self._base_config.fingerprint,
            plan=self._plan,
            policy=self._policy,
            cases=tuple(cases),
        )

    def _config_for_window(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        scenario: StressScenario,
    ) -> BacktestConfig:
        inclusive_end = window_end - timedelta(microseconds=1)
        replay = ReplayConfig(
            symbols=self._base_config.replay.symbols,
            interval=self._base_config.replay.interval,
            start=window_start,
            end=inclusive_end,
            include_warnings=self._base_config.replay.include_warnings,
            page_size=self._base_config.replay.page_size,
        )
        return replace(
            self._base_config,
            replay=replay,
            commission_bps=(
                self._base_config.commission_bps * scenario.cost_multiplier
            ),
            slippage_bps=(
                self._base_config.slippage_bps * scenario.cost_multiplier
            ),
        )


def build_stress_scenarios(
    *,
    cost_multipliers: tuple[Decimal, ...],
    execution_delays: tuple[int, ...],
) -> tuple[StressScenario, ...]:
    if not cost_multipliers or not execution_delays:
        raise RobustnessConfigurationError(
            "Stress grids require cost multipliers and execution delays."
        )
    scenarios = {
        StressScenario(cost_multiplier=cost, execution_delay_candles=delay)
        for cost in cost_multipliers
        for delay in execution_delays
    }
    return tuple(sorted(scenarios, key=lambda item: item.fingerprint))


def _build_strategy(spec: RobustnessStrategySpec) -> Strategy:
    if spec.name == "buy-and-hold":
        return BuyAndHoldStrategy()
    assert spec.short_window is not None
    assert spec.long_window is not None
    return SmaCrossoverStrategy(
        short_window=spec.short_window,
        long_window=spec.long_window,
    )


def _phase_result(
    phase: RobustnessPhase,
    window: ResearchWindow,
    result: BacktestRunResult,
) -> PhaseResult:
    metrics = result.metrics
    return PhaseResult(
        phase=phase,
        window=window,
        event_count=result.replay_result.event_count,
        run_digest=result.run_digest,
        total_return=metrics.total_return,
        maximum_drawdown=metrics.maximum_drawdown,
        sharpe_ratio=metrics.sharpe_ratio,
        annualized_volatility=metrics.annualized_volatility,
        benchmark_return=metrics.benchmark_return,
        commission_cost=metrics.commission_cost,
        slippage_cost=metrics.slippage_cost,
    )


def _failure_reasons(
    *,
    train: PhaseResult,
    validation: PhaseResult,
    test: PhaseResult,
    return_degradation: Decimal,
    policy: RobustnessPolicy,
) -> tuple[str, ...]:
    reasons: list[str] = []
    for phase in (train, validation, test):
        if phase.event_count < policy.minimum_observations:
            reasons.append(
                f"{phase.phase.value} observations below "
                f"{policy.minimum_observations}"
            )
    if test.total_return < policy.minimum_test_return:
        reasons.append("test return below policy minimum")
    if test.maximum_drawdown < policy.maximum_drawdown:
        reasons.append("test drawdown exceeds policy maximum")
    if return_degradation < policy.maximum_return_degradation:
        reasons.append("out-of-sample return degradation exceeds policy")
    return tuple(reasons)


def _optional_difference(
    left: float | None,
    right: float | None,
) -> float | None:
    if left is None or right is None:
        return None
    return left - right
