from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid5

from world_quant_system.data.normalized_models import canonical_json_bytes, format_utc
from world_quant_system.research.models import (
    ResearchError,
    ResearchWindow,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_CASE_NAMESPACE = UUID("0a711759-acde-50d6-b77a-6efe1bddc18e")
_REPORT_NAMESPACE = UUID("2b3f6234-2cf5-5bfe-8fb0-27d0b2f3349e")


class RobustnessError(ResearchError):
    """Base exception for robustness and walk-forward validation failures."""


class RobustnessConfigurationError(RobustnessError):
    """Raised when a robustness plan or policy is invalid."""


class RobustnessInvariantError(RobustnessError):
    """Raised when a deterministic robustness result is internally inconsistent."""


class RobustnessPhase(StrEnum):
    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"


class MarketRegime(StrEnum):
    UPTREND = "uptrend"
    DOWNTREND = "downtrend"
    SIDEWAYS = "sideways"
    HIGH_VOLATILITY_UPTREND = "high_volatility_uptrend"
    HIGH_VOLATILITY_DOWNTREND = "high_volatility_downtrend"
    HIGH_VOLATILITY_SIDEWAYS = "high_volatility_sideways"


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    fold_number: int
    train: ResearchWindow
    validation: ResearchWindow
    test: ResearchWindow

    def __post_init__(self) -> None:
        _require_positive_int(self.fold_number, "Fold number")
        if not all(
            isinstance(window, ResearchWindow)
            for window in (self.train, self.validation, self.test)
        ):
            raise RobustnessConfigurationError(
                "Walk-forward windows must be ResearchWindow values."
            )
        if self.train.end > self.validation.start:
            raise RobustnessConfigurationError(
                "Walk-forward train and validation windows cannot overlap."
            )
        if self.validation.end > self.test.start:
            raise RobustnessConfigurationError(
                "Walk-forward validation and test windows cannot overlap."
            )

    def window(self, phase: RobustnessPhase) -> ResearchWindow:
        if phase is RobustnessPhase.TRAIN:
            return self.train
        if phase is RobustnessPhase.VALIDATION:
            return self.validation
        return self.test

    def to_document(self) -> dict[str, object]:
        return {
            "fold_number": self.fold_number,
            "train": self.train.to_document(),
            "validation": self.validation.to_document(),
            "test": self.test.to_document(),
        }


@dataclass(frozen=True, slots=True)
class WalkForwardPlan:
    folds: tuple[WalkForwardFold, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.folds, tuple) or not self.folds:
            raise RobustnessConfigurationError(
                "Walk-forward plans require a nonempty fold tuple."
            )
        if any(not isinstance(fold, WalkForwardFold) for fold in self.folds):
            raise RobustnessConfigurationError(
                "Walk-forward plans can contain only WalkForwardFold values."
            )
        expected_numbers = tuple(range(1, len(self.folds) + 1))
        if tuple(fold.fold_number for fold in self.folds) != expected_numbers:
            raise RobustnessConfigurationError(
                "Walk-forward fold numbers must be contiguous and start at 1."
            )
        previous_test_end: datetime | None = None
        for fold in self.folds:
            if previous_test_end is not None and fold.test.end <= previous_test_end:
                raise RobustnessConfigurationError(
                    "Walk-forward test windows must advance strictly through time."
                )
            previous_test_end = fold.test.end

    @classmethod
    def rolling(
        cls,
        *,
        start: datetime,
        train_duration: timedelta,
        validation_duration: timedelta,
        test_duration: timedelta,
        step: timedelta,
        fold_count: int,
        anchored_train: bool = False,
    ) -> WalkForwardPlan:
        _require_aware(start, "Walk-forward start")
        for name, value in (
            ("Train duration", train_duration),
            ("Validation duration", validation_duration),
            ("Test duration", test_duration),
            ("Walk-forward step", step),
        ):
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise RobustnessConfigurationError(
                    f"{name} must be a positive timedelta."
                )
        _require_positive_int(fold_count, "Fold count")
        if not isinstance(anchored_train, bool):
            raise RobustnessConfigurationError(
                "Anchored-train setting must be a boolean."
            )
        folds: list[WalkForwardFold] = []
        for index in range(fold_count):
            offset = step * index
            train_start = start if anchored_train else start + offset
            train_end = start + offset + train_duration
            validation_start = train_end
            validation_end = validation_start + validation_duration
            test_start = validation_end
            test_end = test_start + test_duration
            folds.append(
                WalkForwardFold(
                    fold_number=index + 1,
                    train=ResearchWindow(train_start, train_end),
                    validation=ResearchWindow(validation_start, validation_end),
                    test=ResearchWindow(test_start, test_end),
                )
            )
        return cls(tuple(folds))

    @property
    def fingerprint(self) -> str:
        return _sha256(self.to_document())

    def to_document(self) -> dict[str, object]:
        return {"folds": [fold.to_document() for fold in self.folds]}


@dataclass(frozen=True, slots=True)
class RobustnessStrategySpec:
    name: str
    short_window: int | None = None
    long_window: int | None = None

    def __post_init__(self) -> None:
        normalized_name = (
            self.name.strip().lower() if isinstance(self.name, str) else ""
        )
        if normalized_name not in {"buy-and-hold", "sma-crossover"}:
            raise RobustnessConfigurationError(
                "Robustness strategy must be buy-and-hold or sma-crossover."
            )
        object.__setattr__(self, "name", normalized_name)
        if normalized_name == "buy-and-hold":
            if self.short_window is not None or self.long_window is not None:
                raise RobustnessConfigurationError(
                    "Buy-and-hold cannot define SMA windows."
                )
            return
        if self.short_window is None or self.long_window is None:
            raise RobustnessConfigurationError(
                "SMA robustness strategies require short and long windows."
            )
        _require_positive_int(self.short_window, "Short SMA window")
        _require_positive_int(self.long_window, "Long SMA window")
        if self.short_window >= self.long_window:
            raise RobustnessConfigurationError(
                "Short SMA window must be smaller than long SMA window."
            )

    @property
    def fingerprint(self) -> str:
        return _sha256(self.to_document())

    def to_document(self) -> dict[str, object]:
        return {
            "name": self.name,
            "short_window": self.short_window,
            "long_window": self.long_window,
        }


def sma_parameter_grid(
    *,
    base_short_window: int,
    base_long_window: int,
    short_offsets: Iterable[int],
    long_offsets: Iterable[int],
) -> tuple[RobustnessStrategySpec, ...]:
    _require_positive_int(base_short_window, "Base short SMA window")
    _require_positive_int(base_long_window, "Base long SMA window")
    if base_short_window >= base_long_window:
        raise RobustnessConfigurationError(
            "Base short SMA window must be smaller than the long window."
        )
    short_values = _normalized_offsets(short_offsets, "Short-window offset")
    long_values = _normalized_offsets(long_offsets, "Long-window offset")
    specs = {
        RobustnessStrategySpec(
            name="sma-crossover",
            short_window=base_short_window + short_offset,
            long_window=base_long_window + long_offset,
        )
        for short_offset in short_values
        for long_offset in long_values
        if base_short_window + short_offset > 0
        and base_long_window + long_offset > 0
        and base_short_window + short_offset < base_long_window + long_offset
    }
    if not specs:
        raise RobustnessConfigurationError(
            "SMA parameter grid produced no valid parameter combinations."
        )
    return tuple(sorted(specs, key=lambda item: item.fingerprint))


@dataclass(frozen=True, slots=True)
class StressScenario:
    cost_multiplier: Decimal
    execution_delay_candles: int

    def __post_init__(self) -> None:
        _require_finite_decimal(self.cost_multiplier, "Cost multiplier")
        if self.cost_multiplier < _ONE:
            raise RobustnessConfigurationError(
                "Cost stress multiplier cannot be below 1."
            )
        _require_positive_int(
            self.execution_delay_candles,
            "Execution delay candles",
        )

    @property
    def fingerprint(self) -> str:
        return _sha256(self.to_document())

    def to_document(self) -> dict[str, object]:
        return {
            "cost_multiplier": format(self.cost_multiplier, "f"),
            "execution_delay_candles": self.execution_delay_candles,
        }


@dataclass(frozen=True, slots=True)
class RobustnessPolicy:
    minimum_observations: int = 20
    minimum_test_return: Decimal = Decimal("0")
    maximum_drawdown: Decimal = Decimal("-0.50")
    maximum_return_degradation: Decimal = Decimal("-0.25")
    required_pass_rate: Decimal = Decimal("0.60")
    uptrend_threshold: Decimal = Decimal("0.05")
    downtrend_threshold: Decimal = Decimal("-0.05")
    high_volatility_threshold: float = 0.30

    def __post_init__(self) -> None:
        _require_positive_int(self.minimum_observations, "Minimum observations")
        _require_finite_decimal(self.minimum_test_return, "Minimum test return")
        _require_finite_decimal(self.maximum_drawdown, "Maximum drawdown threshold")
        if not Decimal("-1") <= self.maximum_drawdown <= _ZERO:
            raise RobustnessConfigurationError(
                "Maximum drawdown threshold must be between -1 and 0."
            )
        _require_finite_decimal(
            self.maximum_return_degradation,
            "Maximum return degradation",
        )
        if self.maximum_return_degradation > _ZERO:
            raise RobustnessConfigurationError(
                "Maximum return degradation must be nonpositive."
            )
        _require_unit_decimal(self.required_pass_rate, "Required pass rate")
        _require_finite_decimal(self.uptrend_threshold, "Uptrend threshold")
        _require_finite_decimal(self.downtrend_threshold, "Downtrend threshold")
        if self.downtrend_threshold >= self.uptrend_threshold:
            raise RobustnessConfigurationError(
                "Downtrend threshold must be below the uptrend threshold."
            )
        object.__setattr__(
            self,
            "high_volatility_threshold",
            _finite_nonnegative_float(
                self.high_volatility_threshold,
                "High-volatility threshold",
            ),
        )

    @property
    def fingerprint(self) -> str:
        return _sha256(self.to_document())

    def to_document(self) -> dict[str, object]:
        return {
            "minimum_observations": self.minimum_observations,
            "minimum_test_return": format(self.minimum_test_return, "f"),
            "maximum_drawdown": format(self.maximum_drawdown, "f"),
            "maximum_return_degradation": format(
                self.maximum_return_degradation,
                "f",
            ),
            "required_pass_rate": format(self.required_pass_rate, "f"),
            "uptrend_threshold": format(self.uptrend_threshold, "f"),
            "downtrend_threshold": format(self.downtrend_threshold, "f"),
            "high_volatility_threshold": self.high_volatility_threshold,
        }


@dataclass(frozen=True, slots=True)
class PhaseResult:
    phase: RobustnessPhase
    window: ResearchWindow
    event_count: int
    run_digest: str
    total_return: Decimal
    maximum_drawdown: Decimal
    sharpe_ratio: float | None
    annualized_volatility: float | None
    benchmark_return: Decimal | None
    commission_cost: Decimal
    slippage_cost: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.phase, RobustnessPhase):
            raise RobustnessConfigurationError("Phase result has an invalid phase.")
        if not isinstance(self.window, ResearchWindow):
            raise RobustnessConfigurationError("Phase result requires a window.")
        _require_nonnegative_int(self.event_count, "Phase event count")
        _validate_sha256(self.run_digest, "Phase run digest")
        for name, value in (
            ("Phase total return", self.total_return),
            ("Phase maximum drawdown", self.maximum_drawdown),
            ("Phase commission cost", self.commission_cost),
            ("Phase slippage cost", self.slippage_cost),
        ):
            _require_finite_decimal(value, name)
        if self.maximum_drawdown > _ZERO or self.maximum_drawdown < Decimal("-1"):
            raise RobustnessInvariantError(
                "Phase maximum drawdown must be between -1 and 0."
            )
        _require_optional_finite_float(self.sharpe_ratio, "Phase Sharpe ratio")
        _require_optional_finite_float(
            self.annualized_volatility,
            "Phase annualized volatility",
        )
        if self.benchmark_return is not None:
            _require_finite_decimal(self.benchmark_return, "Phase benchmark return")

    def to_document(self) -> dict[str, object]:
        return {
            "phase": self.phase.value,
            "window": self.window.to_document(),
            "event_count": self.event_count,
            "run_digest": self.run_digest,
            "total_return": format(self.total_return, "f"),
            "maximum_drawdown": format(self.maximum_drawdown, "f"),
            "sharpe_ratio": self.sharpe_ratio,
            "annualized_volatility": self.annualized_volatility,
            "benchmark_return": (
                None
                if self.benchmark_return is None
                else format(self.benchmark_return, "f")
            ),
            "commission_cost": format(self.commission_cost, "f"),
            "slippage_cost": format(self.slippage_cost, "f"),
        }


@dataclass(frozen=True, slots=True)
class RobustnessCaseResult:
    case_id: str
    fold_number: int
    strategy: RobustnessStrategySpec
    scenario: StressScenario
    train: PhaseResult
    validation: PhaseResult
    test: PhaseResult
    regime: MarketRegime
    return_degradation: Decimal
    sharpe_degradation: float | None
    passed: bool
    failure_reasons: tuple[str, ...]
    case_digest: str

    def __post_init__(self) -> None:
        _validate_uuid(self.case_id, "Robustness case ID")
        _require_positive_int(self.fold_number, "Robustness case fold number")
        if not isinstance(self.strategy, RobustnessStrategySpec):
            raise RobustnessConfigurationError(
                "Robustness case requires a strategy spec."
            )
        if not isinstance(self.scenario, StressScenario):
            raise RobustnessConfigurationError(
                "Robustness case requires a stress scenario."
            )
        if (self.train.phase, self.validation.phase, self.test.phase) != (
            RobustnessPhase.TRAIN,
            RobustnessPhase.VALIDATION,
            RobustnessPhase.TEST,
        ):
            raise RobustnessInvariantError(
                "Robustness cases require train, validation, and test phases."
            )
        if not isinstance(self.regime, MarketRegime):
            raise RobustnessConfigurationError(
                "Robustness case has an invalid market regime."
            )
        _require_finite_decimal(self.return_degradation, "Return degradation")
        expected_return_degradation = self.test.total_return - self.train.total_return
        if self.return_degradation != expected_return_degradation:
            raise RobustnessInvariantError(
                "Return degradation must equal test return minus train return."
            )
        _require_optional_finite_float(
            self.sharpe_degradation,
            "Sharpe degradation",
        )
        if not isinstance(self.passed, bool):
            raise RobustnessConfigurationError(
                "Robustness case pass state must be boolean."
            )
        if not isinstance(self.failure_reasons, tuple) or any(
            not isinstance(reason, str) or not reason.strip()
            for reason in self.failure_reasons
        ):
            raise RobustnessConfigurationError(
                "Robustness failure reasons must be nonblank strings."
            )
        if self.passed == bool(self.failure_reasons):
            raise RobustnessInvariantError(
                "Passed cases require no failure reasons and failed cases require one."
            )
        _validate_sha256(self.case_digest, "Robustness case digest")
        if self.case_digest != _sha256(self._identity_document()):
            raise RobustnessInvariantError(
                "Robustness case digest does not match its content."
            )
        expected_case_id = str(uuid5(_CASE_NAMESPACE, self.case_digest))
        if self.case_id != expected_case_id:
            raise RobustnessInvariantError(
                "Robustness case ID does not match its deterministic digest."
            )

    @classmethod
    def build(
        cls,
        *,
        fold_number: int,
        strategy: RobustnessStrategySpec,
        scenario: StressScenario,
        train: PhaseResult,
        validation: PhaseResult,
        test: PhaseResult,
        regime: MarketRegime,
        return_degradation: Decimal,
        sharpe_degradation: float | None,
        passed: bool,
        failure_reasons: tuple[str, ...],
    ) -> RobustnessCaseResult:
        document = {
            "fold_number": fold_number,
            "strategy": strategy.to_document(),
            "scenario": scenario.to_document(),
            "train": train.to_document(),
            "validation": validation.to_document(),
            "test": test.to_document(),
            "regime": regime.value,
            "return_degradation": format(return_degradation, "f"),
            "sharpe_degradation": sharpe_degradation,
            "passed": passed,
            "failure_reasons": list(failure_reasons),
        }
        digest = _sha256(document)
        return cls(
            case_id=str(uuid5(_CASE_NAMESPACE, digest)),
            fold_number=fold_number,
            strategy=strategy,
            scenario=scenario,
            train=train,
            validation=validation,
            test=test,
            regime=regime,
            return_degradation=return_degradation,
            sharpe_degradation=sharpe_degradation,
            passed=passed,
            failure_reasons=failure_reasons,
            case_digest=digest,
        )

    def _identity_document(self) -> dict[str, object]:
        return {
            "fold_number": self.fold_number,
            "strategy": self.strategy.to_document(),
            "scenario": self.scenario.to_document(),
            "train": self.train.to_document(),
            "validation": self.validation.to_document(),
            "test": self.test.to_document(),
            "regime": self.regime.value,
            "return_degradation": format(self.return_degradation, "f"),
            "sharpe_degradation": self.sharpe_degradation,
            "passed": self.passed,
            "failure_reasons": list(self.failure_reasons),
        }

    def to_document(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            **self._identity_document(),
            "case_digest": self.case_digest,
        }


@dataclass(frozen=True, slots=True)
class RegimeSummary:
    regime: MarketRegime
    case_count: int
    passed_count: int
    median_test_return: Decimal
    worst_test_return: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.regime, MarketRegime):
            raise RobustnessConfigurationError("Regime summary is invalid.")
        _require_positive_int(self.case_count, "Regime case count")
        _require_nonnegative_int(self.passed_count, "Regime passed count")
        if self.passed_count > self.case_count:
            raise RobustnessInvariantError(
                "Regime passed count cannot exceed its case count."
            )
        _require_finite_decimal(self.median_test_return, "Regime median return")
        _require_finite_decimal(self.worst_test_return, "Regime worst return")

    def to_document(self) -> dict[str, object]:
        return {
            "regime": self.regime.value,
            "case_count": self.case_count,
            "passed_count": self.passed_count,
            "median_test_return": format(self.median_test_return, "f"),
            "worst_test_return": format(self.worst_test_return, "f"),
        }


@dataclass(frozen=True, slots=True)
class RobustnessReport:
    report_id: str
    created_at: datetime
    dataset_digest: str
    base_config_fingerprint: str
    plan: WalkForwardPlan
    policy: RobustnessPolicy
    cases: tuple[RobustnessCaseResult, ...]
    regime_summaries: tuple[RegimeSummary, ...]
    median_test_return: Decimal
    worst_test_return: Decimal
    median_return_degradation: Decimal
    worst_return_degradation: Decimal
    worst_maximum_drawdown: Decimal
    pass_rate: Decimal
    passed: bool
    report_digest: str

    def __post_init__(self) -> None:
        _validate_uuid(self.report_id, "Robustness report ID")
        _require_aware(self.created_at, "Robustness report creation timestamp")
        _validate_sha256(self.dataset_digest, "Robustness dataset digest")
        _validate_sha256(
            self.base_config_fingerprint,
            "Robustness base-config fingerprint",
        )
        if not isinstance(self.plan, WalkForwardPlan):
            raise RobustnessConfigurationError(
                "Robustness report requires a walk-forward plan."
            )
        if not isinstance(self.policy, RobustnessPolicy):
            raise RobustnessConfigurationError(
                "Robustness report requires a policy."
            )
        if not isinstance(self.cases, tuple) or not self.cases:
            raise RobustnessConfigurationError(
                "Robustness report requires a nonempty case tuple."
            )
        if any(not isinstance(case, RobustnessCaseResult) for case in self.cases):
            raise RobustnessConfigurationError(
                "Robustness report contains an invalid case."
            )
        if tuple(sorted(self.cases, key=lambda item: item.case_id)) != self.cases:
            raise RobustnessConfigurationError(
                "Robustness report cases must be sorted by deterministic case ID."
            )
        if not isinstance(self.regime_summaries, tuple) or any(
            not isinstance(item, RegimeSummary) for item in self.regime_summaries
        ):
            raise RobustnessConfigurationError(
                "Robustness regime summaries must be an immutable tuple."
            )
        for name, value in (
            ("Median test return", self.median_test_return),
            ("Worst test return", self.worst_test_return),
            ("Median return degradation", self.median_return_degradation),
            ("Worst return degradation", self.worst_return_degradation),
            ("Worst maximum drawdown", self.worst_maximum_drawdown),
        ):
            _require_finite_decimal(value, name)
        _require_unit_decimal(self.pass_rate, "Robustness pass rate")
        expected_pass_rate = Decimal(
            sum(1 for case in self.cases if case.passed)
        ) / len(self.cases)
        if self.pass_rate != expected_pass_rate:
            raise RobustnessInvariantError(
                "Robustness pass rate does not match the case outcomes."
            )
        if self.passed != (self.pass_rate >= self.policy.required_pass_rate):
            raise RobustnessInvariantError(
                "Robustness report pass state does not match its policy."
            )
        _validate_sha256(self.report_digest, "Robustness report digest")
        if self.report_digest != _sha256(self._identity_document()):
            raise RobustnessInvariantError(
                "Robustness report digest does not match its content."
            )
        if self.report_id != str(uuid5(_REPORT_NAMESPACE, self.report_digest)):
            raise RobustnessInvariantError(
                "Robustness report ID does not match its deterministic digest."
            )

    @classmethod
    def build(
        cls,
        *,
        created_at: datetime,
        dataset_digest: str,
        base_config_fingerprint: str,
        plan: WalkForwardPlan,
        policy: RobustnessPolicy,
        cases: tuple[RobustnessCaseResult, ...],
    ) -> RobustnessReport:
        _require_aware(created_at, "Robustness report creation timestamp")
        if not cases:
            raise RobustnessConfigurationError(
                "Cannot build an empty robustness report."
            )
        ordered_cases = tuple(sorted(cases, key=lambda item: item.case_id))
        test_returns = tuple(case.test.total_return for case in ordered_cases)
        degradations = tuple(case.return_degradation for case in ordered_cases)
        drawdowns = tuple(case.test.maximum_drawdown for case in ordered_cases)
        passed_count = sum(1 for case in ordered_cases if case.passed)
        pass_rate = Decimal(passed_count) / len(ordered_cases)
        summaries = _regime_summaries(ordered_cases)
        median_test_return = _decimal_median(test_returns)
        worst_test_return = min(test_returns)
        median_return_degradation = _decimal_median(degradations)
        worst_return_degradation = min(degradations)
        worst_maximum_drawdown = min(drawdowns)
        passed = pass_rate >= policy.required_pass_rate
        document = {
            "dataset_digest": dataset_digest,
            "base_config_fingerprint": base_config_fingerprint,
            "plan": plan.to_document(),
            "policy": policy.to_document(),
            "cases": [case.to_document() for case in ordered_cases],
            "regime_summaries": [summary.to_document() for summary in summaries],
            "median_test_return": format(median_test_return, "f"),
            "worst_test_return": format(worst_test_return, "f"),
            "median_return_degradation": format(
                median_return_degradation,
                "f",
            ),
            "worst_return_degradation": format(
                worst_return_degradation,
                "f",
            ),
            "worst_maximum_drawdown": format(
                worst_maximum_drawdown,
                "f",
            ),
            "pass_rate": format(pass_rate, "f"),
            "passed": passed,
        }
        digest = _sha256(document)
        return cls(
            report_id=str(uuid5(_REPORT_NAMESPACE, digest)),
            created_at=created_at.astimezone(UTC),
            dataset_digest=dataset_digest,
            base_config_fingerprint=base_config_fingerprint,
            plan=plan,
            policy=policy,
            cases=ordered_cases,
            regime_summaries=summaries,
            median_test_return=median_test_return,
            worst_test_return=worst_test_return,
            median_return_degradation=median_return_degradation,
            worst_return_degradation=worst_return_degradation,
            worst_maximum_drawdown=worst_maximum_drawdown,
            pass_rate=pass_rate,
            passed=passed,
            report_digest=digest,
        )

    def _identity_document(self) -> dict[str, object]:
        return {
            "dataset_digest": self.dataset_digest,
            "base_config_fingerprint": self.base_config_fingerprint,
            "plan": self.plan.to_document(),
            "policy": self.policy.to_document(),
            "cases": [case.to_document() for case in self.cases],
            "regime_summaries": [
                summary.to_document() for summary in self.regime_summaries
            ],
            "median_test_return": format(self.median_test_return, "f"),
            "worst_test_return": format(self.worst_test_return, "f"),
            "median_return_degradation": format(
                self.median_return_degradation,
                "f",
            ),
            "worst_return_degradation": format(
                self.worst_return_degradation,
                "f",
            ),
            "worst_maximum_drawdown": format(
                self.worst_maximum_drawdown,
                "f",
            ),
            "pass_rate": format(self.pass_rate, "f"),
            "passed": self.passed,
        }

    def to_document(self) -> dict[str, object]:
        return {
            "report_id": self.report_id,
            "created_at": format_utc(self.created_at),
            **self._identity_document(),
            "report_digest": self.report_digest,
        }


def classify_market_regime(
    *,
    benchmark_return: Decimal | None,
    fallback_return: Decimal,
    annualized_volatility: float | None,
    policy: RobustnessPolicy,
) -> MarketRegime:
    value = fallback_return if benchmark_return is None else benchmark_return
    high_volatility = (
        annualized_volatility is not None
        and annualized_volatility >= policy.high_volatility_threshold
    )
    if value >= policy.uptrend_threshold:
        return (
            MarketRegime.HIGH_VOLATILITY_UPTREND
            if high_volatility
            else MarketRegime.UPTREND
        )
    if value <= policy.downtrend_threshold:
        return (
            MarketRegime.HIGH_VOLATILITY_DOWNTREND
            if high_volatility
            else MarketRegime.DOWNTREND
        )
    return (
        MarketRegime.HIGH_VOLATILITY_SIDEWAYS
        if high_volatility
        else MarketRegime.SIDEWAYS
    )


def _regime_summaries(
    cases: tuple[RobustnessCaseResult, ...],
) -> tuple[RegimeSummary, ...]:
    summaries: list[RegimeSummary] = []
    for regime in MarketRegime:
        selected = tuple(case for case in cases if case.regime is regime)
        if not selected:
            continue
        returns = tuple(case.test.total_return for case in selected)
        summaries.append(
            RegimeSummary(
                regime=regime,
                case_count=len(selected),
                passed_count=sum(1 for case in selected if case.passed),
                median_test_return=_decimal_median(returns),
                worst_test_return=min(returns),
            )
        )
    return tuple(summaries)


def _normalized_offsets(values: Iterable[int], field_name: str) -> tuple[int, ...]:
    normalized: set[int] = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            raise RobustnessConfigurationError(f"{field_name} must be an integer.")
        normalized.add(value)
    if not normalized:
        raise RobustnessConfigurationError(
            f"{field_name} collection cannot be empty."
        )
    return tuple(sorted(normalized))


def _decimal_median(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        raise RobustnessConfigurationError("Cannot calculate an empty median.")
    ordered = tuple(sorted(values))
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def _sha256(document: object) -> str:
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


def _require_aware(value: object, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise RobustnessConfigurationError(
            f"{field_name} must include timezone information."
        )


def _require_positive_int(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RobustnessConfigurationError(f"{field_name} must be a positive integer.")


def _require_nonnegative_int(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RobustnessConfigurationError(
            f"{field_name} must be a nonnegative integer."
        )


def _require_finite_decimal(value: object, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise RobustnessConfigurationError(f"{field_name} must be a finite Decimal.")


def _require_unit_decimal(value: object, field_name: str) -> None:
    _require_finite_decimal(value, field_name)
    assert isinstance(value, Decimal)
    if not _ZERO <= value <= _ONE:
        raise RobustnessConfigurationError(f"{field_name} must be between 0 and 1.")


def _require_optional_finite_float(value: float | None, field_name: str) -> None:
    if value is not None and not math.isfinite(value):
        raise RobustnessConfigurationError(
            f"{field_name} must be finite when defined."
        )


def _finite_nonnegative_float(value: object, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise RobustnessConfigurationError(
            f"{field_name} must be a finite nonnegative number."
        )
    return float(value)


def _validate_sha256(value: object, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RobustnessConfigurationError(
            f"{field_name} must be a lowercase SHA-256 digest."
        )


def _validate_uuid(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise RobustnessConfigurationError(f"{field_name} must be a UUID string.")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise RobustnessConfigurationError(
            f"{field_name} must be a valid UUID."
        ) from error
    if str(parsed) != value:
        raise RobustnessConfigurationError(
            f"{field_name} must use canonical lowercase UUID form."
        )
