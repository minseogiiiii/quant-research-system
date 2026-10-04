from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final
from uuid import UUID, uuid5

_SCHEMA_VERSION: Final[int] = 1
_REPORT_NAMESPACE: Final[UUID] = UUID("4b5b4c52-ff6d-55f4-854d-9a2e8f2dc617")
_ZERO = Decimal("0")
_ONE = Decimal("1")


class PortfolioPromotionError(Exception):
    """Base exception for research portfolio-promotion failures."""


class PortfolioPromotionConfigurationError(PortfolioPromotionError):
    """Raised when an input, policy, or manifest is invalid."""


class PortfolioPromotionIntegrityError(PortfolioPromotionError):
    """Raised when immutable evidence does not match its declared identity."""


class PortfolioPromotionEligibilityError(PortfolioPromotionError):
    """Raised when evidence is valid but insufficient for portfolio evaluation."""


class AllocationMethod(StrEnum):
    EQUAL_WEIGHT = "equal_weight"
    CAPPED_INVERSE_VOLATILITY = "capped_inverse_volatility"


class PromotionDecision(StrEnum):
    PROMOTED_FOR_SHADOW_RESEARCH = "promoted_for_shadow_research"
    REJECTED = "rejected"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class CandidateDecision(StrEnum):
    ELIGIBLE = "eligible"
    REJECTED = "rejected"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class StressScenarioName(StrEnum):
    BASE = "base"
    ADVERSE_COST = "adverse_cost"
    SEVERE_COST = "severe_cost"
    COMMON_LOSS_SHOCK = "common_loss_shock"
    LARGEST_CANDIDATE_REMOVED = "largest_candidate_removed"


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    candidate_id: str
    strategy_name: str
    strategy_version: str
    dataset_digest: str
    backtest_file_sha256: str
    robustness_file_sha256: str
    robustness_report_digest: str
    robustness_passed: bool
    statistical_file_sha256: str
    statistical_report_digest: str
    statistical_passed: bool
    declared_turnover: Decimal | None = None
    declared_cost_to_gross_profit_ratio: Decimal | None = None

    def __post_init__(self) -> None:
        for text_value, name in (
            (self.candidate_id, "Candidate ID"),
            (self.strategy_name, "Strategy name"),
            (self.strategy_version, "Strategy version"),
        ):
            _require_nonblank(text_value, name)
        for boolean_value, name in (
            (self.robustness_passed, "Robustness passed"),
            (self.statistical_passed, "Statistical validation passed"),
        ):
            if not isinstance(boolean_value, bool):
                raise PortfolioPromotionConfigurationError(
                    f"{name} must be a boolean."
                )
        for digest_value, name in (
            (self.dataset_digest, "Dataset digest"),
            (self.backtest_file_sha256, "Backtest file SHA-256"),
            (self.robustness_file_sha256, "Robustness file SHA-256"),
            (self.robustness_report_digest, "Robustness report digest"),
            (self.statistical_file_sha256, "Statistical file SHA-256"),
            (self.statistical_report_digest, "Statistical report digest"),
        ):
            _require_sha256(digest_value, name)
        if self.declared_turnover is not None:
            _require_nonnegative_decimal(
                self.declared_turnover,
                "Declared turnover",
            )
        if self.declared_cost_to_gross_profit_ratio is not None:
            _require_nonnegative_decimal(
                self.declared_cost_to_gross_profit_ratio,
                "Declared cost-to-gross-profit ratio",
            )

    def to_document(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "dataset_digest": self.dataset_digest,
            "backtest_file_sha256": self.backtest_file_sha256,
            "robustness_file_sha256": self.robustness_file_sha256,
            "robustness_report_digest": self.robustness_report_digest,
            "robustness_passed": self.robustness_passed,
            "statistical_file_sha256": self.statistical_file_sha256,
            "statistical_report_digest": self.statistical_report_digest,
            "statistical_passed": self.statistical_passed,
            "declared_turnover": _optional_decimal_text(self.declared_turnover),
            "declared_cost_to_gross_profit_ratio": _optional_decimal_text(
                self.declared_cost_to_gross_profit_ratio
            ),
        }


@dataclass(frozen=True, slots=True)
class CandidateGateResult:
    candidate_id: str
    decision: CandidateDecision
    reasons: tuple[str, ...]
    observation_count: int
    cumulative_return: Decimal
    maximum_drawdown: Decimal

    def __post_init__(self) -> None:
        _require_nonblank(self.candidate_id, "Candidate ID")
        if not isinstance(self.decision, CandidateDecision):
            raise PortfolioPromotionConfigurationError(
                "Candidate decision must be a CandidateDecision."
            )
        _require_nonnegative_int(self.observation_count, "Observation count")
        _require_finite_decimal(self.cumulative_return, "Cumulative return")
        _require_finite_decimal(self.maximum_drawdown, "Maximum drawdown")
        if self.maximum_drawdown > _ZERO:
            raise PortfolioPromotionConfigurationError(
                "Maximum drawdown cannot be positive."
            )
        if any(not reason.strip() for reason in self.reasons):
            raise PortfolioPromotionConfigurationError(
                "Candidate gate reasons must be nonblank."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "decision": self.decision.value,
            "reasons": list(self.reasons),
            "observation_count": self.observation_count,
            "cumulative_return": _decimal_text(self.cumulative_return),
            "maximum_drawdown": _decimal_text(self.maximum_drawdown),
        }


@dataclass(frozen=True, slots=True)
class CandidateReturnMatrix:
    timestamps: tuple[datetime, ...]
    candidate_ids: tuple[str, ...]
    returns_by_candidate: tuple[tuple[Decimal, ...], ...]
    matrix_digest: str = field(init=False)
    _candidate_index: Mapping[str, int] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if len(self.candidate_ids) < 2:
            raise PortfolioPromotionEligibilityError(
                "At least two candidate return series are required."
            )
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise PortfolioPromotionConfigurationError(
                "Candidate return columns must be unique."
            )
        if tuple(sorted(self.candidate_ids)) != self.candidate_ids:
            raise PortfolioPromotionConfigurationError(
                "Candidate return columns must be sorted for deterministic identity."
            )
        if any(not value.strip() for value in self.candidate_ids):
            raise PortfolioPromotionConfigurationError(
                "Candidate return columns must be nonblank."
            )
        if not self.timestamps:
            raise PortfolioPromotionEligibilityError(
                "Candidate return matrix must contain observations."
            )
        previous: datetime | None = None
        for timestamp in self.timestamps:
            _require_aware(timestamp, "Return timestamp")
            normalized = timestamp.astimezone(UTC)
            if previous is not None and normalized <= previous:
                raise PortfolioPromotionConfigurationError(
                    "Return timestamps must be strictly increasing."
                )
            previous = normalized
        if len(self.returns_by_candidate) != len(self.candidate_ids):
            raise PortfolioPromotionConfigurationError(
                "Return-series count must match candidate count."
            )
        expected = len(self.timestamps)
        for series in self.returns_by_candidate:
            if len(series) != expected:
                raise PortfolioPromotionConfigurationError(
                    "Every candidate must have one return per timestamp."
                )
            for value in series:
                _require_finite_decimal(value, "Periodic return")
                if value <= -_ONE:
                    raise PortfolioPromotionConfigurationError(
                        "Periodic returns must be greater than -1."
                    )
        object.__setattr__(
            self,
            "_candidate_index",
            MappingProxyType(
                {
                    candidate_id: index
                    for index, candidate_id in enumerate(self.candidate_ids)
                }
            ),
        )
        object.__setattr__(
            self,
            "matrix_digest",
            hashlib.sha256(canonical_json_bytes(self.identity_document())).hexdigest(),
        )

    @property
    def observation_count(self) -> int:
        return len(self.timestamps)

    def series_for(self, candidate_id: str) -> tuple[Decimal, ...]:
        try:
            index = self._candidate_index[candidate_id]
        except KeyError as error:
            raise PortfolioPromotionConfigurationError(
                f"Unknown candidate return series: {candidate_id}"
            ) from error
        return self.returns_by_candidate[index]

    def identity_document(self) -> dict[str, object]:
        return {
            "timestamps": [format_utc(value) for value in self.timestamps],
            "candidate_ids": list(self.candidate_ids),
            "returns_by_candidate": [
                [_decimal_text(value) for value in series]
                for series in self.returns_by_candidate
            ],
        }


@dataclass(frozen=True, slots=True)
class CorrelationPair:
    left_candidate_id: str
    right_candidate_id: str
    pearson_correlation: float
    spearman_correlation: float
    loss_period_correlation: float | None
    loss_period_observations: int

    def __post_init__(self) -> None:
        if self.left_candidate_id >= self.right_candidate_id:
            raise PortfolioPromotionConfigurationError(
                "Correlation pairs must use sorted candidate IDs."
            )
        for value, name in (
            (self.pearson_correlation, "Pearson correlation"),
            (self.spearman_correlation, "Spearman correlation"),
        ):
            _require_correlation(value, name)
        if self.loss_period_correlation is not None:
            _require_correlation(
                self.loss_period_correlation,
                "Loss-period correlation",
            )
        _require_nonnegative_int(
            self.loss_period_observations,
            "Loss-period observations",
        )

    def to_document(self) -> dict[str, object]:
        return {
            "left_candidate_id": self.left_candidate_id,
            "right_candidate_id": self.right_candidate_id,
            "pearson_correlation": self.pearson_correlation,
            "spearman_correlation": self.spearman_correlation,
            "loss_period_correlation": self.loss_period_correlation,
            "loss_period_observations": self.loss_period_observations,
        }


@dataclass(frozen=True, slots=True)
class RedundancyCluster:
    cluster_id: str
    candidate_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_sha256(self.cluster_id, "Cluster ID")
        if not self.candidate_ids:
            raise PortfolioPromotionConfigurationError(
                "Redundancy clusters cannot be empty."
            )
        if tuple(sorted(set(self.candidate_ids))) != self.candidate_ids:
            raise PortfolioPromotionConfigurationError(
                "Cluster candidate IDs must be unique and sorted."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "cluster_id": self.cluster_id,
            "candidate_ids": list(self.candidate_ids),
        }


@dataclass(frozen=True, slots=True)
class CandidateWeight:
    candidate_id: str
    weight: Decimal

    def __post_init__(self) -> None:
        _require_nonblank(self.candidate_id, "Candidate ID")
        _require_unit_decimal(self.weight, "Candidate weight")

    def to_document(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "weight": _decimal_text(self.weight),
        }


@dataclass(frozen=True, slots=True)
class AllocationSnapshot:
    timestamp: datetime
    weights: tuple[CandidateWeight, ...]
    cash_weight: Decimal
    one_way_turnover: Decimal

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "Allocation timestamp")
        if tuple(sorted(item.candidate_id for item in self.weights)) != tuple(
            item.candidate_id for item in self.weights
        ):
            raise PortfolioPromotionConfigurationError(
                "Allocation weights must be sorted by candidate ID."
            )
        if len({item.candidate_id for item in self.weights}) != len(self.weights):
            raise PortfolioPromotionConfigurationError(
                "Allocation weights cannot repeat a candidate."
            )
        _require_unit_decimal(self.cash_weight, "Cash weight")
        _require_nonnegative_decimal(self.one_way_turnover, "One-way turnover")
        total = sum((item.weight for item in self.weights), self.cash_weight)
        if abs(total - _ONE) > Decimal("0.000000000001"):
            raise PortfolioPromotionConfigurationError(
                "Allocation weights and cash must sum to one."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "timestamp": format_utc(self.timestamp),
            "weights": [item.to_document() for item in self.weights],
            "cash_weight": _decimal_text(self.cash_weight),
            "one_way_turnover": _decimal_text(self.one_way_turnover),
        }


@dataclass(frozen=True, slots=True)
class PortfolioMetrics:
    observation_count: int
    cumulative_return: Decimal
    annualized_return: float | None
    annualized_volatility: float
    sharpe_ratio: float | None
    sortino_ratio: float | None
    maximum_drawdown: Decimal
    expected_shortfall: Decimal
    total_turnover: Decimal
    total_cost: Decimal
    average_effective_strategy_count: float
    average_cash_weight: Decimal

    def __post_init__(self) -> None:
        _require_positive_int(self.observation_count, "Observation count")
        _require_finite_decimal(self.cumulative_return, "Cumulative return")
        _require_optional_finite_float(self.annualized_return, "Annualized return")
        _require_nonnegative_float(
            self.annualized_volatility,
            "Annualized volatility",
        )
        _require_optional_finite_float(self.sharpe_ratio, "Sharpe ratio")
        _require_optional_finite_float(self.sortino_ratio, "Sortino ratio")
        _require_finite_decimal(self.maximum_drawdown, "Maximum drawdown")
        if self.maximum_drawdown > _ZERO:
            raise PortfolioPromotionConfigurationError(
                "Maximum drawdown cannot be positive."
            )
        _require_finite_decimal(self.expected_shortfall, "Expected shortfall")
        _require_nonnegative_decimal(self.total_turnover, "Total turnover")
        _require_nonnegative_decimal(self.total_cost, "Total cost")
        _require_nonnegative_float(
            self.average_effective_strategy_count,
            "Average effective strategy count",
        )
        _require_unit_decimal(self.average_cash_weight, "Average cash weight")

    def to_document(self) -> dict[str, object]:
        return {
            "observation_count": self.observation_count,
            "cumulative_return": _decimal_text(self.cumulative_return),
            "annualized_return": self.annualized_return,
            "annualized_volatility": self.annualized_volatility,
            "sharpe_ratio": self.sharpe_ratio,
            "sortino_ratio": self.sortino_ratio,
            "maximum_drawdown": _decimal_text(self.maximum_drawdown),
            "expected_shortfall": _decimal_text(self.expected_shortfall),
            "total_turnover": _decimal_text(self.total_turnover),
            "total_cost": _decimal_text(self.total_cost),
            "average_effective_strategy_count": (
                self.average_effective_strategy_count
            ),
            "average_cash_weight": _decimal_text(self.average_cash_weight),
        }


@dataclass(frozen=True, slots=True)
class PortfolioFoldResult:
    fold_number: int
    start_at: datetime
    end_at: datetime
    metrics: PortfolioMetrics
    passed: bool
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_positive_int(self.fold_number, "Fold number")
        _require_aware(self.start_at, "Fold start")
        _require_aware(self.end_at, "Fold end")
        if self.end_at < self.start_at:
            raise PortfolioPromotionConfigurationError(
                "Fold end cannot precede fold start."
            )
        if not isinstance(self.metrics, PortfolioMetrics):
            raise PortfolioPromotionConfigurationError(
                "Fold metrics must be PortfolioMetrics."
            )
        if not isinstance(self.passed, bool):
            raise PortfolioPromotionConfigurationError(
                "Fold passed must be a boolean."
            )
        if any(not reason.strip() for reason in self.reasons):
            raise PortfolioPromotionConfigurationError(
                "Fold reasons must be nonblank."
            )
        if self.passed != (not self.reasons):
            raise PortfolioPromotionConfigurationError(
                "Fold pass state must agree with its reasons."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "fold_number": self.fold_number,
            "start_at": format_utc(self.start_at),
            "end_at": format_utc(self.end_at),
            "metrics": self.metrics.to_document(),
            "passed": self.passed,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class PortfolioWalkForwardResult:
    fold_count: int
    pass_rate: Decimal
    worst_cumulative_return: Decimal
    worst_maximum_drawdown: Decimal
    passed: bool
    folds: tuple[PortfolioFoldResult, ...]

    def __post_init__(self) -> None:
        _require_positive_int(self.fold_count, "Fold count")
        if len(self.folds) != self.fold_count:
            raise PortfolioPromotionConfigurationError(
                "Fold count must match stored fold results."
            )
        if tuple(item.fold_number for item in self.folds) != tuple(
            range(1, self.fold_count + 1)
        ):
            raise PortfolioPromotionConfigurationError(
                "Walk-forward fold numbers must be sequential."
            )
        for previous, current in zip(
            self.folds,
            self.folds[1:],
            strict=False,
        ):
            if current.start_at <= previous.end_at:
                raise PortfolioPromotionConfigurationError(
                    "Walk-forward folds must advance without overlap."
                )
        _require_unit_decimal(self.pass_rate, "Fold pass rate")
        expected_pass_rate = Decimal(sum(item.passed for item in self.folds)) / Decimal(
            self.fold_count
        )
        if self.pass_rate != expected_pass_rate:
            raise PortfolioPromotionConfigurationError(
                "Walk-forward pass rate does not match the folds."
            )
        _require_finite_decimal(
            self.worst_cumulative_return,
            "Worst fold cumulative return",
        )
        _require_finite_decimal(
            self.worst_maximum_drawdown,
            "Worst fold maximum drawdown",
        )
        if self.worst_maximum_drawdown > _ZERO:
            raise PortfolioPromotionConfigurationError(
                "Worst fold drawdown cannot be positive."
            )
        if self.worst_cumulative_return != min(
            item.metrics.cumulative_return for item in self.folds
        ):
            raise PortfolioPromotionConfigurationError(
                "Worst fold return does not match the folds."
            )
        if self.worst_maximum_drawdown != min(
            item.metrics.maximum_drawdown for item in self.folds
        ):
            raise PortfolioPromotionConfigurationError(
                "Worst fold drawdown does not match the folds."
            )
        if not isinstance(self.passed, bool):
            raise PortfolioPromotionConfigurationError(
                "Walk-forward passed must be a boolean."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "fold_count": self.fold_count,
            "pass_rate": _decimal_text(self.pass_rate),
            "worst_cumulative_return": _decimal_text(
                self.worst_cumulative_return
            ),
            "worst_maximum_drawdown": _decimal_text(
                self.worst_maximum_drawdown
            ),
            "passed": self.passed,
            "folds": [item.to_document() for item in self.folds],
        }


@dataclass(frozen=True, slots=True)
class PortfolioScenarioResult:
    scenario: StressScenarioName
    cost_bps: Decimal
    common_loss_multiplier: Decimal
    removed_candidate_id: str | None
    metrics: PortfolioMetrics
    passed: bool
    reasons: tuple[str, ...]
    periodic_returns_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.scenario, StressScenarioName):
            raise PortfolioPromotionConfigurationError(
                "Scenario must be a StressScenarioName."
            )
        _require_nonnegative_decimal(self.cost_bps, "Scenario cost bps")
        if self.common_loss_multiplier < _ONE:
            raise PortfolioPromotionConfigurationError(
                "Common-loss multiplier must be at least one."
            )
        if self.removed_candidate_id is not None:
            _require_nonblank(
                self.removed_candidate_id,
                "Removed candidate ID",
            )
        if not isinstance(self.metrics, PortfolioMetrics):
            raise PortfolioPromotionConfigurationError(
                "Scenario metrics must be PortfolioMetrics."
            )
        if not isinstance(self.passed, bool):
            raise PortfolioPromotionConfigurationError(
                "Scenario passed must be a boolean."
            )
        if any(not reason.strip() for reason in self.reasons):
            raise PortfolioPromotionConfigurationError(
                "Scenario reasons must be nonblank."
            )
        if self.passed != (not self.reasons):
            raise PortfolioPromotionConfigurationError(
                "Scenario pass state must agree with its reasons."
            )
        _require_sha256(
            self.periodic_returns_digest,
            "Periodic-returns digest",
        )

    def to_document(self) -> dict[str, object]:
        return {
            "scenario": self.scenario.value,
            "cost_bps": _decimal_text(self.cost_bps),
            "common_loss_multiplier": _decimal_text(
                self.common_loss_multiplier
            ),
            "removed_candidate_id": self.removed_candidate_id,
            "metrics": self.metrics.to_document(),
            "passed": self.passed,
            "reasons": list(self.reasons),
            "periodic_returns_digest": self.periodic_returns_digest,
        }


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    annualization_periods: int = 252
    minimum_candidate_observations: int = 60
    minimum_eligible_candidates: int = 2
    minimum_candidate_cumulative_return: Decimal = Decimal("0")
    maximum_candidate_drawdown: Decimal = Decimal("-0.40")
    maximum_declared_turnover: Decimal = Decimal("100")
    maximum_cost_to_gross_profit_ratio: Decimal = Decimal("0.70")
    volatility_lookback: int = 20
    rebalance_frequency: int = 5
    minimum_cash_weight: Decimal = Decimal("0.05")
    maximum_candidate_weight: Decimal = Decimal("0.40")
    maximum_cluster_weight: Decimal = Decimal("0.60")
    maximum_pairwise_correlation: float = 0.90
    maximum_loss_period_correlation: float = 0.90
    minimum_loss_period_observations: int = 8
    base_cost_bps: Decimal = Decimal("0")
    adverse_cost_bps: Decimal = Decimal("10")
    severe_cost_bps: Decimal = Decimal("25")
    common_loss_multiplier: Decimal = Decimal("1.50")
    minimum_base_cumulative_return: Decimal = Decimal("0")
    minimum_adverse_cumulative_return: Decimal = Decimal("0")
    maximum_severe_drawdown: Decimal = Decimal("-0.35")
    minimum_effective_strategy_count: float = 1.50
    walk_forward_folds: int = 4
    minimum_fold_observations: int = 15
    minimum_fold_cumulative_return: Decimal = Decimal("-0.05")
    maximum_fold_drawdown: Decimal = Decimal("-0.25")
    minimum_fold_pass_rate: Decimal = Decimal("0.75")
    expected_shortfall_fraction: Decimal = Decimal("0.05")

    def __post_init__(self) -> None:
        _require_positive_int(
            self.annualization_periods,
            "Annualization periods",
        )
        _require_positive_int(
            self.minimum_candidate_observations,
            "Minimum candidate observations",
        )
        _require_positive_int(
            self.minimum_eligible_candidates,
            "Minimum eligible candidates",
        )
        _require_positive_int(self.volatility_lookback, "Volatility lookback")
        _require_positive_int(
            self.rebalance_frequency,
            "Rebalance frequency",
        )
        _require_positive_int(
            self.minimum_loss_period_observations,
            "Minimum loss-period observations",
        )
        _require_positive_int(self.walk_forward_folds, "Walk-forward folds")
        _require_positive_int(
            self.minimum_fold_observations,
            "Minimum fold observations",
        )
        for finite_decimal_value, name in (
            (
                self.minimum_candidate_cumulative_return,
                "Minimum candidate cumulative return",
            ),
            (
                self.maximum_candidate_drawdown,
                "Maximum candidate drawdown",
            ),
            (
                self.minimum_base_cumulative_return,
                "Minimum base cumulative return",
            ),
            (
                self.minimum_adverse_cumulative_return,
                "Minimum adverse cumulative return",
            ),
            (self.maximum_severe_drawdown, "Maximum severe drawdown"),
            (
                self.minimum_fold_cumulative_return,
                "Minimum fold cumulative return",
            ),
            (self.maximum_fold_drawdown, "Maximum fold drawdown"),
        ):
            _require_finite_decimal(finite_decimal_value, name)
        if self.maximum_candidate_drawdown > _ZERO:
            raise PortfolioPromotionConfigurationError(
                "Maximum candidate drawdown must be zero or negative."
            )
        if self.maximum_severe_drawdown > _ZERO:
            raise PortfolioPromotionConfigurationError(
                "Maximum severe drawdown must be zero or negative."
            )
        if self.maximum_fold_drawdown > _ZERO:
            raise PortfolioPromotionConfigurationError(
                "Maximum fold drawdown must be zero or negative."
            )
        for nonnegative_decimal_value, name in (
            (self.maximum_declared_turnover, "Maximum declared turnover"),
            (
                self.maximum_cost_to_gross_profit_ratio,
                "Maximum cost-to-gross-profit ratio",
            ),
            (self.base_cost_bps, "Base cost bps"),
            (self.adverse_cost_bps, "Adverse cost bps"),
            (self.severe_cost_bps, "Severe cost bps"),
        ):
            _require_nonnegative_decimal(nonnegative_decimal_value, name)
        _require_unit_decimal(self.minimum_cash_weight, "Minimum cash weight")
        for unit_decimal_value, name in (
            (self.maximum_candidate_weight, "Maximum candidate weight"),
            (self.maximum_cluster_weight, "Maximum cluster weight"),
            (self.minimum_fold_pass_rate, "Minimum fold pass rate"),
            (
                self.expected_shortfall_fraction,
                "Expected-shortfall fraction",
            ),
        ):
            _require_open_closed_unit_decimal(unit_decimal_value, name)
        if self.minimum_cash_weight >= _ONE:
            raise PortfolioPromotionConfigurationError(
                "Minimum cash weight must be below one."
            )
        if self.maximum_candidate_weight > self.maximum_cluster_weight:
            raise PortfolioPromotionConfigurationError(
                "Candidate weight cap cannot exceed cluster weight cap."
            )
        for correlation_value, name in (
            (
                self.maximum_pairwise_correlation,
                "Maximum pairwise correlation",
            ),
            (
                self.maximum_loss_period_correlation,
                "Maximum loss-period correlation",
            ),
        ):
            _require_correlation(correlation_value, name)
        if self.common_loss_multiplier < _ONE:
            raise PortfolioPromotionConfigurationError(
                "Common-loss multiplier must be at least one."
            )
        _require_positive_float(
            self.minimum_effective_strategy_count,
            "Minimum effective strategy count",
        )
        if not (
            self.base_cost_bps
            <= self.adverse_cost_bps
            <= self.severe_cost_bps
        ):
            raise PortfolioPromotionConfigurationError(
                "Scenario costs must be nondecreasing."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "annualization_periods": self.annualization_periods,
            "minimum_candidate_observations": (
                self.minimum_candidate_observations
            ),
            "minimum_eligible_candidates": self.minimum_eligible_candidates,
            "minimum_candidate_cumulative_return": _decimal_text(
                self.minimum_candidate_cumulative_return
            ),
            "maximum_candidate_drawdown": _decimal_text(
                self.maximum_candidate_drawdown
            ),
            "maximum_declared_turnover": _decimal_text(
                self.maximum_declared_turnover
            ),
            "maximum_cost_to_gross_profit_ratio": _decimal_text(
                self.maximum_cost_to_gross_profit_ratio
            ),
            "volatility_lookback": self.volatility_lookback,
            "rebalance_frequency": self.rebalance_frequency,
            "minimum_cash_weight": _decimal_text(self.minimum_cash_weight),
            "maximum_candidate_weight": _decimal_text(
                self.maximum_candidate_weight
            ),
            "maximum_cluster_weight": _decimal_text(
                self.maximum_cluster_weight
            ),
            "maximum_pairwise_correlation": self.maximum_pairwise_correlation,
            "maximum_loss_period_correlation": (
                self.maximum_loss_period_correlation
            ),
            "minimum_loss_period_observations": (
                self.minimum_loss_period_observations
            ),
            "base_cost_bps": _decimal_text(self.base_cost_bps),
            "adverse_cost_bps": _decimal_text(self.adverse_cost_bps),
            "severe_cost_bps": _decimal_text(self.severe_cost_bps),
            "common_loss_multiplier": _decimal_text(
                self.common_loss_multiplier
            ),
            "minimum_base_cumulative_return": _decimal_text(
                self.minimum_base_cumulative_return
            ),
            "minimum_adverse_cumulative_return": _decimal_text(
                self.minimum_adverse_cumulative_return
            ),
            "maximum_severe_drawdown": _decimal_text(
                self.maximum_severe_drawdown
            ),
            "minimum_effective_strategy_count": (
                self.minimum_effective_strategy_count
            ),
            "walk_forward_folds": self.walk_forward_folds,
            "minimum_fold_observations": self.minimum_fold_observations,
            "minimum_fold_cumulative_return": _decimal_text(
                self.minimum_fold_cumulative_return
            ),
            "maximum_fold_drawdown": _decimal_text(
                self.maximum_fold_drawdown
            ),
            "minimum_fold_pass_rate": _decimal_text(
                self.minimum_fold_pass_rate
            ),
            "expected_shortfall_fraction": _decimal_text(
                self.expected_shortfall_fraction
            ),
        }


@dataclass(frozen=True, slots=True)
class PortfolioPromotionReport:
    dataset_digest: str
    matrix_digest: str
    allocation_method: AllocationMethod
    policy: PromotionPolicy
    candidate_evidence: tuple[CandidateEvidence, ...]
    candidate_results: tuple[CandidateGateResult, ...]
    correlations: tuple[CorrelationPair, ...]
    redundancy_clusters: tuple[RedundancyCluster, ...]
    allocation_snapshots: tuple[AllocationSnapshot, ...]
    walk_forward: PortfolioWalkForwardResult
    scenarios: tuple[PortfolioScenarioResult, ...]
    decision: PromotionDecision
    decision_reasons: tuple[str, ...]
    created_at: datetime
    report_id: str = field(init=False)
    report_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.dataset_digest, "Dataset digest")
        _require_sha256(self.matrix_digest, "Matrix digest")
        if not isinstance(self.allocation_method, AllocationMethod):
            raise PortfolioPromotionConfigurationError(
                "Allocation method must be an AllocationMethod."
            )
        if not isinstance(self.policy, PromotionPolicy):
            raise PortfolioPromotionConfigurationError(
                "Portfolio report policy must be PromotionPolicy."
            )
        if not isinstance(self.walk_forward, PortfolioWalkForwardResult):
            raise PortfolioPromotionConfigurationError(
                "Portfolio report requires walk-forward results."
            )
        if self.walk_forward.passed != (
            self.walk_forward.pass_rate >= self.policy.minimum_fold_pass_rate
        ):
            raise PortfolioPromotionConfigurationError(
                "Walk-forward pass state conflicts with the report policy."
            )
        if tuple(item.candidate_id for item in self.candidate_evidence) != tuple(
            sorted(item.candidate_id for item in self.candidate_evidence)
        ):
            raise PortfolioPromotionConfigurationError(
                "Candidate evidence must be sorted by candidate ID."
            )
        if tuple(item.candidate_id for item in self.candidate_results) != tuple(
            sorted(item.candidate_id for item in self.candidate_results)
        ):
            raise PortfolioPromotionConfigurationError(
                "Candidate results must be sorted by candidate ID."
            )
        expected_scenarios = tuple(StressScenarioName)
        if tuple(item.scenario for item in self.scenarios) != expected_scenarios:
            raise PortfolioPromotionConfigurationError(
                "Portfolio report must contain every stress scenario in order."
            )
        if not isinstance(self.decision, PromotionDecision):
            raise PortfolioPromotionConfigurationError(
                "Decision must be a PromotionDecision."
            )
        if any(not reason.strip() for reason in self.decision_reasons):
            raise PortfolioPromotionConfigurationError(
                "Decision reasons must be nonblank."
            )
        promoted = self.decision is PromotionDecision.PROMOTED_FOR_SHADOW_RESEARCH
        if promoted == bool(self.decision_reasons):
            raise PortfolioPromotionConfigurationError(
                "Promotion decision must agree with its reasons."
            )
        _require_aware(self.created_at, "Report creation timestamp")
        digest = hashlib.sha256(
            canonical_json_bytes(self.identity_document())
        ).hexdigest()
        object.__setattr__(self, "report_digest", digest)
        object.__setattr__(
            self,
            "report_id",
            str(uuid5(_REPORT_NAMESPACE, digest)),
        )

    def identity_document(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "dataset_digest": self.dataset_digest,
            "matrix_digest": self.matrix_digest,
            "allocation_method": self.allocation_method.value,
            "policy": self.policy.to_document(),
            "candidate_evidence": [
                item.to_document() for item in self.candidate_evidence
            ],
            "candidate_results": [
                item.to_document() for item in self.candidate_results
            ],
            "correlations": [item.to_document() for item in self.correlations],
            "redundancy_clusters": [
                item.to_document() for item in self.redundancy_clusters
            ],
            "allocation_snapshots": [
                item.to_document() for item in self.allocation_snapshots
            ],
            "walk_forward": self.walk_forward.to_document(),
            "scenarios": [item.to_document() for item in self.scenarios],
            "decision": self.decision.value,
            "decision_reasons": list(self.decision_reasons),
        }

    def to_document(self) -> dict[str, object]:
        return {
            **self.identity_document(),
            "created_at": format_utc(self.created_at),
            "report_id": self.report_id,
            "report_digest": self.report_digest,
            "network_access": "DISABLED",
            "broker_provider": "NONE",
            "live_trading": "DISABLED",
            "order_submission": "DISABLED",
        }


def canonical_json_bytes(document: object) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def format_utc(value: datetime) -> str:
    _require_aware(value, "Timestamp")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _optional_decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return _decimal_text(value)


def _require_nonblank(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise PortfolioPromotionConfigurationError(
            f"{field_name} cannot be empty."
        )


def _require_sha256(value: object, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value.lower() != value
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise PortfolioPromotionConfigurationError(
            f"{field_name} must be a lowercase SHA-256 digest."
        )


def _require_aware(value: object, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise PortfolioPromotionConfigurationError(
            f"{field_name} must include timezone information."
        )


def _require_finite_decimal(value: object, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise PortfolioPromotionConfigurationError(
            f"{field_name} must be a finite Decimal."
        )


def _require_nonnegative_decimal(value: object, field_name: str) -> None:
    _require_finite_decimal(value, field_name)
    assert isinstance(value, Decimal)
    if value < _ZERO:
        raise PortfolioPromotionConfigurationError(
            f"{field_name} cannot be negative."
        )


def _require_unit_decimal(value: object, field_name: str) -> None:
    _require_finite_decimal(value, field_name)
    assert isinstance(value, Decimal)
    if value < _ZERO or value > _ONE:
        raise PortfolioPromotionConfigurationError(
            f"{field_name} must be between zero and one."
        )


def _require_open_closed_unit_decimal(value: object, field_name: str) -> None:
    _require_finite_decimal(value, field_name)
    assert isinstance(value, Decimal)
    if value <= _ZERO or value > _ONE:
        raise PortfolioPromotionConfigurationError(
            f"{field_name} must be greater than zero and at most one."
        )


def _require_nonnegative_int(value: object, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PortfolioPromotionConfigurationError(
            f"{field_name} must be a nonnegative integer."
        )


def _require_positive_int(value: object, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PortfolioPromotionConfigurationError(
            f"{field_name} must be a positive integer."
        )


def _require_correlation(value: object, field_name: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < -1.0
        or float(value) > 1.0
    ):
        raise PortfolioPromotionConfigurationError(
            f"{field_name} must be finite and between -1 and 1."
        )


def _require_nonnegative_float(value: object, field_name: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise PortfolioPromotionConfigurationError(
            f"{field_name} must be a finite nonnegative number."
        )


def _require_positive_float(value: object, field_name: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise PortfolioPromotionConfigurationError(
            f"{field_name} must be a finite positive number."
        )


def _require_optional_finite_float(
    value: object,
    field_name: str,
) -> None:
    if value is None:
        return
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise PortfolioPromotionConfigurationError(
            f"{field_name} must be finite when supplied."
        )


__all__ = [
    "AllocationMethod",
    "AllocationSnapshot",
    "CandidateDecision",
    "CandidateEvidence",
    "CandidateGateResult",
    "CandidateReturnMatrix",
    "CandidateWeight",
    "CorrelationPair",
    "PortfolioFoldResult",
    "PortfolioMetrics",
    "PortfolioPromotionConfigurationError",
    "PortfolioPromotionEligibilityError",
    "PortfolioPromotionError",
    "PortfolioPromotionIntegrityError",
    "PortfolioPromotionReport",
    "PortfolioScenarioResult",
    "PortfolioWalkForwardResult",
    "PromotionDecision",
    "PromotionPolicy",
    "RedundancyCluster",
    "StressScenarioName",
    "canonical_json_bytes",
    "format_utc",
]
