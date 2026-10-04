from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid5

from world_quant_system.data.normalized_models import canonical_json_bytes, format_utc
from world_quant_system.research.models import ResearchError

_ZERO = Decimal("0")
_ONE = Decimal("1")
_REPORT_NAMESPACE = UUID("8ca54bdd-c288-57cb-9a4b-64dd3a692bb5")


class StatisticalValidationError(ResearchError):
    """Base exception for statistical validation failures."""


class StatisticalValidationConfigurationError(StatisticalValidationError):
    """Raised when statistical validation input or policy is invalid."""


class StatisticalValidationEligibilityError(StatisticalValidationError):
    """Raised when the supplied trials are not statistically eligible."""


class StatisticalValidationIntegrityError(StatisticalValidationError):
    """Raised when a deterministic statistical result is inconsistent."""


class StatisticalTrialStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class FailedStatisticalTrial:
    trial_id: str
    failure_reason: str
    experiment_id: str | None = None

    def __post_init__(self) -> None:
        _require_nonblank(self.trial_id, "Failed-trial ID")
        _require_nonblank(self.failure_reason, "Failed-trial reason")
        if self.experiment_id is not None:
            _validate_uuid(self.experiment_id, "Failed-trial experiment ID")

    def to_document(self) -> dict[str, object]:
        return {
            "trial_id": self.trial_id,
            "status": StatisticalTrialStatus.FAILED.value,
            "failure_reason": self.failure_reason,
            "experiment_id": self.experiment_id,
        }


@dataclass(frozen=True, slots=True)
class StatisticalReturnsMatrix:
    timestamps: tuple[datetime, ...]
    trial_ids: tuple[str, ...]
    returns_by_trial: tuple[tuple[Decimal, ...], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.timestamps, tuple) or not self.timestamps:
            raise StatisticalValidationConfigurationError(
                "Statistical returns require a nonempty timestamp tuple."
            )
        previous: datetime | None = None
        for timestamp in self.timestamps:
            _require_aware(timestamp, "Return timestamp")
            if previous is not None and timestamp <= previous:
                raise StatisticalValidationConfigurationError(
                    "Return timestamps must be strictly increasing."
                )
            previous = timestamp
        if not isinstance(self.trial_ids, tuple) or len(self.trial_ids) < 2:
            raise StatisticalValidationConfigurationError(
                "Statistical validation requires at least two successful trials."
            )
        if len(set(self.trial_ids)) != len(self.trial_ids):
            raise StatisticalValidationConfigurationError(
                "Statistical trial IDs must be unique."
            )
        for trial_id in self.trial_ids:
            _require_nonblank(trial_id, "Statistical trial ID")
        if (
            not isinstance(self.returns_by_trial, tuple)
            or len(self.returns_by_trial) != len(self.trial_ids)
        ):
            raise StatisticalValidationConfigurationError(
                "Return-series count must match the trial-ID count."
            )
        for series in self.returns_by_trial:
            if not isinstance(series, tuple) or len(series) != len(self.timestamps):
                raise StatisticalValidationConfigurationError(
                    "Every trial must have one return for every timestamp."
                )
            for value in series:
                _require_finite_decimal(value, "Periodic return")
                if value <= Decimal("-1"):
                    raise StatisticalValidationConfigurationError(
                        "Periodic returns must be greater than -1."
                    )

    @property
    def observation_count(self) -> int:
        return len(self.timestamps)

    @property
    def successful_trial_count(self) -> int:
        return len(self.trial_ids)

    @property
    def matrix_digest(self) -> str:
        return _sha256(self.to_document())

    def series_for(self, trial_id: str) -> tuple[Decimal, ...]:
        try:
            index = self.trial_ids.index(trial_id)
        except ValueError as error:
            raise StatisticalValidationConfigurationError(
                "Selected trial is not present in the return matrix."
            ) from error
        return self.returns_by_trial[index]

    def to_document(self) -> dict[str, object]:
        return {
            "timestamps": [format_utc(value) for value in self.timestamps],
            "trials": [
                {
                    "trial_id": trial_id,
                    "returns": [format(value, "f") for value in series],
                }
                for trial_id, series in zip(
                    self.trial_ids,
                    self.returns_by_trial,
                    strict=True,
                )
            ],
        }


@dataclass(frozen=True, slots=True)
class StatisticalValidationPolicy:
    annualization_periods: int = 252
    minimum_observations: int = 60
    cscv_partitions: int = 8
    significance_level: Decimal = Decimal("0.05")
    false_discovery_rate: Decimal = Decimal("0.05")
    minimum_deflated_sharpe_probability: Decimal = Decimal("0.95")
    maximum_probability_backtest_overfitting: Decimal = Decimal("0.05")
    ranking_split_fraction: Decimal = Decimal("0.50")
    minimum_rank_correlation: float = 0.0
    require_bonferroni_pass: bool = True
    require_false_discovery_pass: bool = True

    def __post_init__(self) -> None:
        _require_positive_int(self.annualization_periods, "Annualization periods")
        if self.minimum_observations < 8:
            raise StatisticalValidationConfigurationError(
                "Minimum observations must be at least 8."
            )
        if (
            isinstance(self.cscv_partitions, bool)
            or not isinstance(self.cscv_partitions, int)
            or self.cscv_partitions < 4
            or self.cscv_partitions > 16
            or self.cscv_partitions % 2 != 0
        ):
            raise StatisticalValidationConfigurationError(
                "CSCV partitions must be an even integer from 4 through 16."
            )
        for name, value in (
            ("Significance level", self.significance_level),
            ("False-discovery rate", self.false_discovery_rate),
            (
                "Minimum deflated-Sharpe probability",
                self.minimum_deflated_sharpe_probability,
            ),
            (
                "Maximum probability of backtest overfitting",
                self.maximum_probability_backtest_overfitting,
            ),
            ("Ranking split fraction", self.ranking_split_fraction),
        ):
            _require_open_unit_decimal(value, name)
        if not isinstance(self.minimum_rank_correlation, float | int):
            raise StatisticalValidationConfigurationError(
                "Minimum rank correlation must be numeric."
            )
        correlation = float(self.minimum_rank_correlation)
        if not math.isfinite(correlation) or correlation < -1 or correlation > 1:
            raise StatisticalValidationConfigurationError(
                "Minimum rank correlation must be between -1 and 1."
            )
        object.__setattr__(self, "minimum_rank_correlation", correlation)
        if not isinstance(self.require_bonferroni_pass, bool):
            raise StatisticalValidationConfigurationError(
                "Bonferroni requirement must be boolean."
            )
        if not isinstance(self.require_false_discovery_pass, bool):
            raise StatisticalValidationConfigurationError(
                "False-discovery requirement must be boolean."
            )

    @property
    def fingerprint(self) -> str:
        return _sha256(self.to_document())

    def to_document(self) -> dict[str, object]:
        return {
            "annualization_periods": self.annualization_periods,
            "minimum_observations": self.minimum_observations,
            "cscv_partitions": self.cscv_partitions,
            "significance_level": format(self.significance_level, "f"),
            "false_discovery_rate": format(self.false_discovery_rate, "f"),
            "minimum_deflated_sharpe_probability": format(
                self.minimum_deflated_sharpe_probability,
                "f",
            ),
            "maximum_probability_backtest_overfitting": format(
                self.maximum_probability_backtest_overfitting,
                "f",
            ),
            "ranking_split_fraction": format(self.ranking_split_fraction, "f"),
            "minimum_rank_correlation": self.minimum_rank_correlation,
            "require_bonferroni_pass": self.require_bonferroni_pass,
            "require_false_discovery_pass": self.require_false_discovery_pass,
        }


@dataclass(frozen=True, slots=True)
class TrialStatistics:
    trial_id: str
    observation_count: int
    mean_return: Decimal
    standard_deviation: float
    period_sharpe_ratio: float
    annualized_sharpe_ratio: float
    skewness: float
    kurtosis: float
    probabilistic_sharpe_probability: Decimal
    one_sided_p_value: Decimal
    bonferroni_passed: bool
    false_discovery_q_value: Decimal
    false_discovery_passed: bool

    def __post_init__(self) -> None:
        _require_nonblank(self.trial_id, "Trial-statistics ID")
        _require_positive_int(self.observation_count, "Trial observation count")
        _require_finite_decimal(self.mean_return, "Trial mean return")
        for name, float_value in (
            ("Trial standard deviation", self.standard_deviation),
            ("Trial period Sharpe ratio", self.period_sharpe_ratio),
            ("Trial annualized Sharpe ratio", self.annualized_sharpe_ratio),
            ("Trial skewness", self.skewness),
            ("Trial kurtosis", self.kurtosis),
        ):
            _require_finite_float(float_value, name)
        if self.standard_deviation <= 0:
            raise StatisticalValidationIntegrityError(
                "Trial standard deviation must be positive."
            )
        for name, decimal_value in (
            (
                "Probabilistic-Sharpe probability",
                self.probabilistic_sharpe_probability,
            ),
            ("One-sided p-value", self.one_sided_p_value),
            ("False-discovery q-value", self.false_discovery_q_value),
        ):
            _require_closed_unit_decimal(decimal_value, name)
        if not isinstance(self.bonferroni_passed, bool):
            raise StatisticalValidationConfigurationError(
                "Bonferroni pass state must be boolean."
            )
        if not isinstance(self.false_discovery_passed, bool):
            raise StatisticalValidationConfigurationError(
                "False-discovery pass state must be boolean."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "trial_id": self.trial_id,
            "observation_count": self.observation_count,
            "mean_return": format(self.mean_return, "f"),
            "standard_deviation": self.standard_deviation,
            "period_sharpe_ratio": self.period_sharpe_ratio,
            "annualized_sharpe_ratio": self.annualized_sharpe_ratio,
            "skewness": self.skewness,
            "kurtosis": self.kurtosis,
            "probabilistic_sharpe_probability": format(
                self.probabilistic_sharpe_probability,
                "f",
            ),
            "one_sided_p_value": format(self.one_sided_p_value, "f"),
            "bonferroni_passed": self.bonferroni_passed,
            "false_discovery_q_value": format(
                self.false_discovery_q_value,
                "f",
            ),
            "false_discovery_passed": self.false_discovery_passed,
        }


@dataclass(frozen=True, slots=True)
class DeflatedSharpeResult:
    selected_trial_id: str
    attempted_trial_count: int
    successful_trial_count: int
    effective_trial_count: int
    sharpe_variance: float
    expected_maximum_period_sharpe: float
    selected_period_sharpe: float
    deflated_sharpe_probability: Decimal
    passed: bool

    def __post_init__(self) -> None:
        _require_nonblank(self.selected_trial_id, "Selected trial ID")
        _require_positive_int(self.attempted_trial_count, "Attempted trial count")
        _require_positive_int(self.successful_trial_count, "Successful trial count")
        _require_positive_int(self.effective_trial_count, "Effective trial count")
        if self.successful_trial_count > self.attempted_trial_count:
            raise StatisticalValidationIntegrityError(
                "Successful trial count cannot exceed attempted trials."
            )
        if self.effective_trial_count > self.attempted_trial_count:
            raise StatisticalValidationIntegrityError(
                "Effective trial count cannot exceed attempted trials."
            )
        for name, value in (
            ("Sharpe variance", self.sharpe_variance),
            ("Expected maximum period Sharpe", self.expected_maximum_period_sharpe),
            ("Selected period Sharpe", self.selected_period_sharpe),
        ):
            _require_finite_float(value, name)
        if self.sharpe_variance < 0:
            raise StatisticalValidationIntegrityError(
                "Sharpe variance cannot be negative."
            )
        _require_closed_unit_decimal(
            self.deflated_sharpe_probability,
            "Deflated-Sharpe probability",
        )
        if not isinstance(self.passed, bool):
            raise StatisticalValidationConfigurationError(
                "Deflated-Sharpe pass state must be boolean."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "selected_trial_id": self.selected_trial_id,
            "attempted_trial_count": self.attempted_trial_count,
            "successful_trial_count": self.successful_trial_count,
            "effective_trial_count": self.effective_trial_count,
            "sharpe_variance": self.sharpe_variance,
            "expected_maximum_period_sharpe": self.expected_maximum_period_sharpe,
            "selected_period_sharpe": self.selected_period_sharpe,
            "deflated_sharpe_probability": format(
                self.deflated_sharpe_probability,
                "f",
            ),
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class CscvSplitResult:
    split_number: int
    in_sample_partitions: tuple[int, ...]
    selected_trial_id: str
    in_sample_sharpe: float
    out_of_sample_sharpe: float
    out_of_sample_rank: float
    relative_rank: float
    logit: float
    out_of_sample_loss: bool

    def __post_init__(self) -> None:
        _require_positive_int(self.split_number, "CSCV split number")
        if (
            not isinstance(self.in_sample_partitions, tuple)
            or not self.in_sample_partitions
        ):
            raise StatisticalValidationConfigurationError(
                "CSCV split requires in-sample partitions."
            )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in self.in_sample_partitions
        ):
            raise StatisticalValidationConfigurationError(
                "CSCV partition indexes must be nonnegative integers."
            )
        _require_nonblank(self.selected_trial_id, "CSCV selected trial ID")
        for name, value in (
            ("CSCV in-sample Sharpe", self.in_sample_sharpe),
            ("CSCV out-of-sample Sharpe", self.out_of_sample_sharpe),
            ("CSCV out-of-sample rank", self.out_of_sample_rank),
            ("CSCV relative rank", self.relative_rank),
            ("CSCV logit", self.logit),
        ):
            _require_finite_float(value, name)
        if not 0 < self.relative_rank < 1:
            raise StatisticalValidationIntegrityError(
                "CSCV relative rank must be strictly between 0 and 1."
            )
        if not isinstance(self.out_of_sample_loss, bool):
            raise StatisticalValidationConfigurationError(
                "CSCV loss state must be boolean."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "split_number": self.split_number,
            "in_sample_partitions": list(self.in_sample_partitions),
            "selected_trial_id": self.selected_trial_id,
            "in_sample_sharpe": self.in_sample_sharpe,
            "out_of_sample_sharpe": self.out_of_sample_sharpe,
            "out_of_sample_rank": self.out_of_sample_rank,
            "relative_rank": self.relative_rank,
            "logit": self.logit,
            "out_of_sample_loss": self.out_of_sample_loss,
        }


@dataclass(frozen=True, slots=True)
class ProbabilityBacktestOverfittingResult:
    partition_count: int
    combination_count: int
    probability_backtest_overfitting: Decimal
    probability_out_of_sample_loss: Decimal
    median_logit: float
    performance_degradation_slope: float | None
    passed: bool
    splits: tuple[CscvSplitResult, ...]

    def __post_init__(self) -> None:
        _require_positive_int(self.partition_count, "PBO partition count")
        _require_positive_int(self.combination_count, "PBO combination count")
        if (
            not isinstance(self.splits, tuple)
            or len(self.splits) != self.combination_count
        ):
            raise StatisticalValidationConfigurationError(
                "PBO split count must match its combination count."
            )
        for name, value in (
            (
                "Probability of backtest overfitting",
                self.probability_backtest_overfitting,
            ),
            ("Probability of out-of-sample loss", self.probability_out_of_sample_loss),
        ):
            _require_closed_unit_decimal(value, name)
        _require_finite_float(self.median_logit, "PBO median logit")
        if self.performance_degradation_slope is not None:
            _require_finite_float(
                self.performance_degradation_slope,
                "PBO performance-degradation slope",
            )
        if not isinstance(self.passed, bool):
            raise StatisticalValidationConfigurationError(
                "PBO pass state must be boolean."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "partition_count": self.partition_count,
            "combination_count": self.combination_count,
            "probability_backtest_overfitting": format(
                self.probability_backtest_overfitting,
                "f",
            ),
            "probability_out_of_sample_loss": format(
                self.probability_out_of_sample_loss,
                "f",
            ),
            "median_logit": self.median_logit,
            "performance_degradation_slope": self.performance_degradation_slope,
            "passed": self.passed,
            "splits": [split.to_document() for split in self.splits],
        }


@dataclass(frozen=True, slots=True)
class RankStabilityResult:
    selected_in_sample_trial_id: str
    selected_out_of_sample_rank: float
    selected_out_of_sample_relative_rank: float
    spearman_rank_correlation: float
    rank_reversal: bool
    passed: bool

    def __post_init__(self) -> None:
        _require_nonblank(
            self.selected_in_sample_trial_id,
            "Rank-stability selected trial ID",
        )
        for name, value in (
            ("Selected OOS rank", self.selected_out_of_sample_rank),
            (
                "Selected OOS relative rank",
                self.selected_out_of_sample_relative_rank,
            ),
            ("Spearman rank correlation", self.spearman_rank_correlation),
        ):
            _require_finite_float(value, name)
        if not 0 < self.selected_out_of_sample_relative_rank < 1:
            raise StatisticalValidationIntegrityError(
                "Selected OOS relative rank must be strictly between 0 and 1."
            )
        if not -1 <= self.spearman_rank_correlation <= 1:
            raise StatisticalValidationIntegrityError(
                "Spearman rank correlation must be between -1 and 1."
            )
        if (
            not isinstance(self.rank_reversal, bool)
            or not isinstance(self.passed, bool)
        ):
            raise StatisticalValidationConfigurationError(
                "Rank-stability states must be boolean."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "selected_in_sample_trial_id": self.selected_in_sample_trial_id,
            "selected_out_of_sample_rank": self.selected_out_of_sample_rank,
            "selected_out_of_sample_relative_rank": (
                self.selected_out_of_sample_relative_rank
            ),
            "spearman_rank_correlation": self.spearman_rank_correlation,
            "rank_reversal": self.rank_reversal,
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class StatisticalRegistryAudit:
    search_id: str
    declared_total_trials: int
    registered_count: int
    succeeded_count: int
    failed_count: int
    pending_count: int
    complete: bool
    experiment_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_nonblank(self.search_id, "Registry search ID")
        for name, value in (
            ("Declared total trials", self.declared_total_trials),
            ("Registered trial count", self.registered_count),
            ("Succeeded trial count", self.succeeded_count),
            ("Failed trial count", self.failed_count),
            ("Pending trial count", self.pending_count),
        ):
            _require_nonnegative_int(value, name)
        if self.declared_total_trials <= 0:
            raise StatisticalValidationConfigurationError(
                "Declared total trials must be positive."
            )
        if (
            self.succeeded_count + self.failed_count + self.pending_count
            != self.registered_count
        ):
            raise StatisticalValidationIntegrityError(
                "Registry outcome counts must equal registered trials."
            )
        if self.registered_count > self.declared_total_trials:
            raise StatisticalValidationIntegrityError(
                "Registered trials cannot exceed declared total trials."
            )
        if not isinstance(self.complete, bool):
            raise StatisticalValidationConfigurationError(
                "Registry completeness must be boolean."
            )
        if self.complete != (
            self.registered_count == self.declared_total_trials
            and self.pending_count == 0
        ):
            raise StatisticalValidationIntegrityError(
                "Registry completeness does not match trial counts."
            )
        if not isinstance(self.experiment_ids, tuple):
            raise StatisticalValidationConfigurationError(
                "Registry experiment IDs must be an immutable tuple."
            )
        for experiment_id in self.experiment_ids:
            _validate_uuid(experiment_id, "Registry experiment ID")
        if tuple(sorted(self.experiment_ids)) != self.experiment_ids:
            raise StatisticalValidationConfigurationError(
                "Registry experiment IDs must be sorted."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "search_id": self.search_id,
            "declared_total_trials": self.declared_total_trials,
            "registered_count": self.registered_count,
            "succeeded_count": self.succeeded_count,
            "failed_count": self.failed_count,
            "pending_count": self.pending_count,
            "complete": self.complete,
            "experiment_ids": list(self.experiment_ids),
        }


@dataclass(frozen=True, slots=True)
class StatisticalValidationReport:
    report_id: str
    created_at: datetime
    dataset_digest: str
    search_id: str
    matrix_digest: str
    policy: StatisticalValidationPolicy
    attempted_trial_count: int
    effective_trial_count: int
    selected_trial_id: str
    trial_statistics: tuple[TrialStatistics, ...]
    failed_trials: tuple[FailedStatisticalTrial, ...]
    omitted_trial_count: int
    deflated_sharpe: DeflatedSharpeResult
    probability_backtest_overfitting: ProbabilityBacktestOverfittingResult
    rank_stability: RankStabilityResult
    registry_audit: StatisticalRegistryAudit | None
    warnings: tuple[str, ...]
    passed: bool
    report_digest: str

    def __post_init__(self) -> None:
        _validate_uuid(self.report_id, "Statistical report ID")
        _require_aware(self.created_at, "Statistical report creation timestamp")
        _validate_sha256(self.dataset_digest, "Statistical dataset digest")
        _require_nonblank(self.search_id, "Statistical search ID")
        _validate_sha256(self.matrix_digest, "Statistical matrix digest")
        if not isinstance(self.policy, StatisticalValidationPolicy):
            raise StatisticalValidationConfigurationError(
                "Statistical report requires a validation policy."
            )
        _require_positive_int(self.attempted_trial_count, "Attempted trial count")
        _require_positive_int(self.effective_trial_count, "Effective trial count")
        if self.effective_trial_count > self.attempted_trial_count:
            raise StatisticalValidationIntegrityError(
                "Effective trials cannot exceed attempted trials."
            )
        _require_nonblank(self.selected_trial_id, "Selected trial ID")
        if (
            not isinstance(self.trial_statistics, tuple)
            or len(self.trial_statistics) < 2
        ):
            raise StatisticalValidationConfigurationError(
                "Statistical report requires at least two trial-statistics records."
            )
        if (
            tuple(sorted(self.trial_statistics, key=lambda item: item.trial_id))
            != self.trial_statistics
        ):
            raise StatisticalValidationConfigurationError(
                "Trial-statistics records must be sorted by trial ID."
            )
        if self.selected_trial_id not in {
            item.trial_id for item in self.trial_statistics
        }:
            raise StatisticalValidationIntegrityError(
                "Selected trial must exist in trial statistics."
            )
        if not isinstance(self.failed_trials, tuple):
            raise StatisticalValidationConfigurationError(
                "Failed trials must be an immutable tuple."
            )
        if (
            tuple(sorted(self.failed_trials, key=lambda item: item.trial_id))
            != self.failed_trials
        ):
            raise StatisticalValidationConfigurationError(
                "Failed trials must be sorted by trial ID."
            )
        _require_nonnegative_int(self.omitted_trial_count, "Omitted trial count")
        observed_count = len(self.trial_statistics) + len(self.failed_trials)
        if observed_count + self.omitted_trial_count != self.attempted_trial_count:
            raise StatisticalValidationIntegrityError(
                "Observed, failed, and omitted trials must equal attempted trials."
            )
        if not isinstance(self.deflated_sharpe, DeflatedSharpeResult):
            raise StatisticalValidationConfigurationError(
                "Statistical report requires a deflated-Sharpe result."
            )
        if not isinstance(
            self.probability_backtest_overfitting,
            ProbabilityBacktestOverfittingResult,
        ):
            raise StatisticalValidationConfigurationError(
                "Statistical report requires a PBO result."
            )
        if not isinstance(self.rank_stability, RankStabilityResult):
            raise StatisticalValidationConfigurationError(
                "Statistical report requires a rank-stability result."
            )
        if self.registry_audit is not None and not isinstance(
            self.registry_audit,
            StatisticalRegistryAudit,
        ):
            raise StatisticalValidationConfigurationError(
                "Registry audit has an invalid type."
            )
        if not isinstance(self.warnings, tuple) or any(
            not isinstance(value, str) or not value.strip()
            for value in self.warnings
        ):
            raise StatisticalValidationConfigurationError(
                "Statistical warnings must be nonblank strings."
            )
        if not isinstance(self.passed, bool):
            raise StatisticalValidationConfigurationError(
                "Statistical report pass state must be boolean."
            )
        _validate_sha256(self.report_digest, "Statistical report digest")
        if self.report_digest != _sha256(self._identity_document()):
            raise StatisticalValidationIntegrityError(
                "Statistical report digest does not match its content."
            )
        if self.report_id != str(uuid5(_REPORT_NAMESPACE, self.report_digest)):
            raise StatisticalValidationIntegrityError(
                "Statistical report ID does not match its deterministic digest."
            )

    @classmethod
    def build(
        cls,
        *,
        created_at: datetime,
        dataset_digest: str,
        search_id: str,
        matrix_digest: str,
        policy: StatisticalValidationPolicy,
        attempted_trial_count: int,
        effective_trial_count: int,
        selected_trial_id: str,
        trial_statistics: tuple[TrialStatistics, ...],
        failed_trials: tuple[FailedStatisticalTrial, ...],
        deflated_sharpe: DeflatedSharpeResult,
        probability_backtest_overfitting: ProbabilityBacktestOverfittingResult,
        rank_stability: RankStabilityResult,
        registry_audit: StatisticalRegistryAudit | None,
        warnings: tuple[str, ...],
    ) -> StatisticalValidationReport:
        _require_aware(created_at, "Statistical report creation timestamp")
        ordered_statistics = tuple(
            sorted(trial_statistics, key=lambda item: item.trial_id)
        )
        ordered_failed = tuple(sorted(failed_trials, key=lambda item: item.trial_id))
        omitted = attempted_trial_count - len(ordered_statistics) - len(ordered_failed)
        if omitted < 0:
            raise StatisticalValidationConfigurationError(
                "Attempted trial count is below observed and failed trials."
            )
        selected = next(
            (item for item in ordered_statistics if item.trial_id == selected_trial_id),
            None,
        )
        if selected is None:
            raise StatisticalValidationConfigurationError(
                "Selected trial does not exist in trial statistics."
            )
        multiplicity_passed = (
            (not policy.require_bonferroni_pass or selected.bonferroni_passed)
            and (
                not policy.require_false_discovery_pass
                or selected.false_discovery_passed
            )
        )
        registry_passed = registry_audit is None or registry_audit.complete
        passed = (
            deflated_sharpe.passed
            and probability_backtest_overfitting.passed
            and rank_stability.passed
            and multiplicity_passed
            and registry_passed
            and omitted == 0
        )
        document = {
            "dataset_digest": dataset_digest,
            "search_id": search_id,
            "matrix_digest": matrix_digest,
            "policy": policy.to_document(),
            "attempted_trial_count": attempted_trial_count,
            "effective_trial_count": effective_trial_count,
            "selected_trial_id": selected_trial_id,
            "trial_statistics": [item.to_document() for item in ordered_statistics],
            "failed_trials": [item.to_document() for item in ordered_failed],
            "omitted_trial_count": omitted,
            "deflated_sharpe": deflated_sharpe.to_document(),
            "probability_backtest_overfitting": (
                probability_backtest_overfitting.to_document()
            ),
            "rank_stability": rank_stability.to_document(),
            "registry_audit": (
                None if registry_audit is None else registry_audit.to_document()
            ),
            "warnings": list(warnings),
            "passed": passed,
        }
        digest = _sha256(document)
        return cls(
            report_id=str(uuid5(_REPORT_NAMESPACE, digest)),
            created_at=created_at.astimezone(UTC),
            dataset_digest=dataset_digest,
            search_id=search_id,
            matrix_digest=matrix_digest,
            policy=policy,
            attempted_trial_count=attempted_trial_count,
            effective_trial_count=effective_trial_count,
            selected_trial_id=selected_trial_id,
            trial_statistics=ordered_statistics,
            failed_trials=ordered_failed,
            omitted_trial_count=omitted,
            deflated_sharpe=deflated_sharpe,
            probability_backtest_overfitting=probability_backtest_overfitting,
            rank_stability=rank_stability,
            registry_audit=registry_audit,
            warnings=warnings,
            passed=passed,
            report_digest=digest,
        )

    def _identity_document(self) -> dict[str, object]:
        return {
            "dataset_digest": self.dataset_digest,
            "search_id": self.search_id,
            "matrix_digest": self.matrix_digest,
            "policy": self.policy.to_document(),
            "attempted_trial_count": self.attempted_trial_count,
            "effective_trial_count": self.effective_trial_count,
            "selected_trial_id": self.selected_trial_id,
            "trial_statistics": [item.to_document() for item in self.trial_statistics],
            "failed_trials": [item.to_document() for item in self.failed_trials],
            "omitted_trial_count": self.omitted_trial_count,
            "deflated_sharpe": self.deflated_sharpe.to_document(),
            "probability_backtest_overfitting": (
                self.probability_backtest_overfitting.to_document()
            ),
            "rank_stability": self.rank_stability.to_document(),
            "registry_audit": (
                None
                if self.registry_audit is None
                else self.registry_audit.to_document()
            ),
            "warnings": list(self.warnings),
            "passed": self.passed,
        }

    def to_document(self) -> dict[str, object]:
        return {
            "report_id": self.report_id,
            "created_at": format_utc(self.created_at),
            **self._identity_document(),
            "report_digest": self.report_digest,
        }


def _sha256(document: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


def _require_nonblank(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise StatisticalValidationConfigurationError(
            f"{field_name} must be a nonblank string."
        )


def _require_positive_int(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StatisticalValidationConfigurationError(
            f"{field_name} must be a positive integer."
        )


def _require_nonnegative_int(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StatisticalValidationConfigurationError(
            f"{field_name} must be a nonnegative integer."
        )


def _require_aware(value: object, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise StatisticalValidationConfigurationError(
            f"{field_name} must be timezone-aware."
        )


def _require_finite_decimal(value: object, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise StatisticalValidationConfigurationError(
            f"{field_name} must be a finite Decimal."
        )


def _require_finite_float(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, float | int):
        raise StatisticalValidationConfigurationError(
            f"{field_name} must be numeric."
        )
    if not math.isfinite(float(value)):
        raise StatisticalValidationConfigurationError(
            f"{field_name} must be finite."
        )


def _require_open_unit_decimal(value: object, field_name: str) -> None:
    _require_finite_decimal(value, field_name)
    assert isinstance(value, Decimal)
    if not _ZERO < value < _ONE:
        raise StatisticalValidationConfigurationError(
            f"{field_name} must be strictly between 0 and 1."
        )


def _require_closed_unit_decimal(value: object, field_name: str) -> None:
    _require_finite_decimal(value, field_name)
    assert isinstance(value, Decimal)
    if not _ZERO <= value <= _ONE:
        raise StatisticalValidationConfigurationError(
            f"{field_name} must be between 0 and 1."
        )


def _validate_sha256(value: object, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise StatisticalValidationConfigurationError(
            f"{field_name} must be a lowercase SHA-256 digest."
        )


def _validate_uuid(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise StatisticalValidationConfigurationError(
            f"{field_name} must be a UUID string."
        )
    try:
        UUID(value)
    except ValueError as error:
        raise StatisticalValidationConfigurationError(
            f"{field_name} must be a valid UUID."
        ) from error
