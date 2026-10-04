from __future__ import annotations

import csv
import itertools
import math
import statistics
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import NormalDist

from world_quant_system.research.models import ExperimentStatus
from world_quant_system.research.registry import SQLiteExperimentRegistry
from world_quant_system.research.statistical_validation_models import (
    CscvSplitResult,
    DeflatedSharpeResult,
    FailedStatisticalTrial,
    ProbabilityBacktestOverfittingResult,
    RankStabilityResult,
    StatisticalRegistryAudit,
    StatisticalReturnsMatrix,
    StatisticalValidationConfigurationError,
    StatisticalValidationEligibilityError,
    StatisticalValidationPolicy,
    StatisticalValidationReport,
    TrialStatistics,
)

_EULER_MASCHERONI = 0.5772156649015329
_NORMAL = NormalDist()


def parse_statistical_returns_csv(path: Path | str) -> StatisticalReturnsMatrix:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise StatisticalValidationConfigurationError(
            f"Statistical returns CSV does not exist: {source}"
        )
    try:
        with source.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration as error:
                raise StatisticalValidationConfigurationError(
                    "Statistical returns CSV is empty."
                ) from error
            if len(header) < 3 or header[0].strip().lower() != "timestamp":
                raise StatisticalValidationConfigurationError(
                    "Returns CSV must use timestamp followed by at least two trial IDs."
                )
            trial_ids = tuple(value.strip() for value in header[1:])
            if any(not value for value in trial_ids):
                raise StatisticalValidationConfigurationError(
                    "Returns CSV trial IDs must be nonblank."
                )
            if len(set(trial_ids)) != len(trial_ids):
                raise StatisticalValidationConfigurationError(
                    "Returns CSV trial IDs must be unique."
                )
            timestamps: list[datetime] = []
            columns: list[list[Decimal]] = [[] for _ in trial_ids]
            expected_width = len(header)
            for line_number, row in enumerate(reader, start=2):
                if not row or all(not value.strip() for value in row):
                    raise StatisticalValidationConfigurationError(
                        f"Returns CSV contains a blank row at line {line_number}."
                    )
                if len(row) != expected_width:
                    raise StatisticalValidationConfigurationError(
                        f"Returns CSV line {line_number} has the wrong column count."
                    )
                timestamp = _parse_timestamp(row[0], line_number)
                if timestamps and timestamp <= timestamps[-1]:
                    raise StatisticalValidationConfigurationError(
                        "Returns CSV timestamps must be strictly increasing."
                    )
                timestamps.append(timestamp)
                for index, raw_value in enumerate(row[1:]):
                    columns[index].append(
                        _parse_return(raw_value, line_number, trial_ids[index])
                    )
    except UnicodeDecodeError as error:
        raise StatisticalValidationConfigurationError(
            "Returns CSV must be UTF-8 encoded."
        ) from error
    return StatisticalReturnsMatrix(
        timestamps=tuple(timestamps),
        trial_ids=trial_ids,
        returns_by_trial=tuple(tuple(column) for column in columns),
    )


async def build_registry_audit(
    *,
    registry: SQLiteExperimentRegistry,
    search_id: str,
    matrix_trial_ids: tuple[str, ...],
) -> tuple[StatisticalRegistryAudit, tuple[FailedStatisticalTrial, ...]]:
    snapshots = tuple(
        snapshot
        for snapshot in await registry.query(limit=10_000)
        if snapshot.record.spec.search_audit.search_id == search_id
    )
    if not snapshots:
        raise StatisticalValidationEligibilityError(
            "Experiment registry contains no trials for the requested search ID."
        )
    declared_counts = {
        snapshot.record.spec.search_audit.total_trials for snapshot in snapshots
    }
    if len(declared_counts) != 1:
        raise StatisticalValidationEligibilityError(
            "Registry trials disagree on the declared total trial count."
        )
    trial_numbers = tuple(
        snapshot.record.spec.search_audit.trial_number for snapshot in snapshots
    )
    if len(set(trial_numbers)) != len(trial_numbers):
        raise StatisticalValidationEligibilityError(
            "Registry search contains duplicate trial numbers."
        )
    succeeded: list[str] = []
    failed: list[FailedStatisticalTrial] = []
    pending = 0
    for snapshot in snapshots:
        experiment_id = snapshot.record.experiment_id
        outcome = snapshot.outcome
        if outcome is None:
            pending += 1
        elif outcome.status is ExperimentStatus.SUCCEEDED:
            succeeded.append(experiment_id)
        else:
            failed.append(
                FailedStatisticalTrial(
                    trial_id=experiment_id,
                    experiment_id=experiment_id,
                    failure_reason=outcome.failure_reason or "unspecified failure",
                )
            )
    if set(succeeded) != set(matrix_trial_ids):
        missing_returns = sorted(set(succeeded) - set(matrix_trial_ids))
        unknown_columns = sorted(set(matrix_trial_ids) - set(succeeded))
        details: list[str] = []
        if missing_returns:
            details.append(f"missing successful trials: {', '.join(missing_returns)}")
        if unknown_columns:
            details.append(f"unregistered return columns: {', '.join(unknown_columns)}")
        raise StatisticalValidationEligibilityError(
            "Returns matrix does not match successful registry outcomes ("
            + "; ".join(details)
            + ")."
        )
    declared_total = next(iter(declared_counts))
    ordered_ids = tuple(sorted(snapshot.record.experiment_id for snapshot in snapshots))
    audit = StatisticalRegistryAudit(
        search_id=search_id,
        declared_total_trials=declared_total,
        registered_count=len(snapshots),
        succeeded_count=len(succeeded),
        failed_count=len(failed),
        pending_count=pending,
        complete=len(snapshots) == declared_total and pending == 0,
        experiment_ids=ordered_ids,
    )
    return audit, tuple(sorted(failed, key=lambda item: item.trial_id))


class DeterministicStatisticalValidator:
    def __init__(
        self,
        *,
        matrix: StatisticalReturnsMatrix,
        dataset_digest: str,
        search_id: str,
        attempted_trial_count: int,
        effective_trial_count: int | None = None,
        selected_trial_id: str | None = None,
        failed_trials: tuple[FailedStatisticalTrial, ...] = (),
        registry_audit: StatisticalRegistryAudit | None = None,
        policy: StatisticalValidationPolicy | None = None,
    ) -> None:
        self._matrix = matrix
        self._dataset_digest = dataset_digest
        self._search_id = search_id.strip() if isinstance(search_id, str) else ""
        self._attempted_trial_count = attempted_trial_count
        self._effective_trial_count = (
            attempted_trial_count
            if effective_trial_count is None
            else effective_trial_count
        )
        self._selected_trial_id = selected_trial_id
        self._failed_trials = failed_trials
        self._registry_audit = registry_audit
        self._policy = policy or StatisticalValidationPolicy()
        self._validate_configuration()

    def run(
        self,
        *,
        created_at: datetime | None = None,
    ) -> StatisticalValidationReport:
        statistics_without_multiplicity = tuple(
            _base_trial_statistics(
                trial_id,
                series,
                annualization_periods=self._policy.annualization_periods,
            )
            for trial_id, series in zip(
                self._matrix.trial_ids,
                self._matrix.returns_by_trial,
                strict=True,
            )
        )
        selected_trial_id = self._selected_trial_id or max(
            statistics_without_multiplicity,
            key=lambda item: (item.period_sharpe_ratio, item.trial_id),
        ).trial_id
        p_values = {
            item.trial_id: item.one_sided_p_value
            for item in statistics_without_multiplicity
        }
        q_values = _benjamini_hochberg_q_values(p_values)
        bonferroni_threshold = self._policy.significance_level / Decimal(
            self._attempted_trial_count
        )
        trial_statistics = tuple(
            TrialStatistics(
                trial_id=item.trial_id,
                observation_count=item.observation_count,
                mean_return=item.mean_return,
                standard_deviation=item.standard_deviation,
                period_sharpe_ratio=item.period_sharpe_ratio,
                annualized_sharpe_ratio=item.annualized_sharpe_ratio,
                skewness=item.skewness,
                kurtosis=item.kurtosis,
                probabilistic_sharpe_probability=(
                    item.probabilistic_sharpe_probability
                ),
                one_sided_p_value=item.one_sided_p_value,
                bonferroni_passed=item.one_sided_p_value <= bonferroni_threshold,
                false_discovery_q_value=q_values[item.trial_id],
                false_discovery_passed=(
                    q_values[item.trial_id] <= self._policy.false_discovery_rate
                ),
            )
            for item in statistics_without_multiplicity
        )
        deflated_sharpe = _deflated_sharpe_result(
            trial_statistics=trial_statistics,
            selected_trial_id=selected_trial_id,
            attempted_trial_count=self._attempted_trial_count,
            effective_trial_count=self._effective_trial_count,
            policy=self._policy,
        )
        pbo = _probability_backtest_overfitting(
            matrix=self._matrix,
            policy=self._policy,
        )
        rank_stability = _rank_stability(
            matrix=self._matrix,
            policy=self._policy,
        )
        warnings = _warnings(
            attempted_trial_count=self._attempted_trial_count,
            effective_trial_count=self._effective_trial_count,
            successful_trial_count=self._matrix.successful_trial_count,
            failed_trial_count=len(self._failed_trials),
            registry_audit=self._registry_audit,
        )
        return StatisticalValidationReport.build(
            created_at=created_at or datetime.now(UTC),
            dataset_digest=self._dataset_digest,
            search_id=self._search_id,
            matrix_digest=self._matrix.matrix_digest,
            policy=self._policy,
            attempted_trial_count=self._attempted_trial_count,
            effective_trial_count=self._effective_trial_count,
            selected_trial_id=selected_trial_id,
            trial_statistics=trial_statistics,
            failed_trials=self._failed_trials,
            deflated_sharpe=deflated_sharpe,
            probability_backtest_overfitting=pbo,
            rank_stability=rank_stability,
            registry_audit=self._registry_audit,
            warnings=warnings,
        )

    def _validate_configuration(self) -> None:
        if not self._search_id:
            raise StatisticalValidationConfigurationError(
                "Statistical search ID must be nonblank."
            )
        _validate_digest(self._dataset_digest, "Statistical dataset digest")
        if (
            isinstance(self._attempted_trial_count, bool)
            or not isinstance(self._attempted_trial_count, int)
            or self._attempted_trial_count <= 0
        ):
            raise StatisticalValidationConfigurationError(
                "Attempted trial count must be a positive integer."
            )
        if (
            isinstance(self._effective_trial_count, bool)
            or not isinstance(self._effective_trial_count, int)
            or self._effective_trial_count <= 0
            or self._effective_trial_count > self._attempted_trial_count
        ):
            raise StatisticalValidationConfigurationError(
                "Effective trial count must be positive and no greater than "
                "attempted trials."
            )
        represented = self._matrix.successful_trial_count + len(self._failed_trials)
        if represented > self._attempted_trial_count:
            raise StatisticalValidationConfigurationError(
                "Attempted trial count is below successful and failed trials."
            )
        failed_ids = tuple(item.trial_id for item in self._failed_trials)
        if len(set(failed_ids)) != len(failed_ids):
            raise StatisticalValidationConfigurationError(
                "Failed trial IDs must be unique."
            )
        if set(failed_ids) & set(self._matrix.trial_ids):
            raise StatisticalValidationConfigurationError(
                "A trial cannot be both successful and failed."
            )
        if self._selected_trial_id is not None and self._selected_trial_id not in set(
            self._matrix.trial_ids
        ):
            raise StatisticalValidationConfigurationError(
                "Selected trial is absent from the successful return matrix."
            )
        if self._matrix.observation_count < self._policy.minimum_observations:
            raise StatisticalValidationEligibilityError(
                "Returns matrix does not meet the minimum observation requirement."
            )
        if self._matrix.observation_count % self._policy.cscv_partitions != 0:
            raise StatisticalValidationEligibilityError(
                "Observation count must be divisible by the CSCV partition count."
            )
        partition_size = (
            self._matrix.observation_count // self._policy.cscv_partitions
        )
        if partition_size < 4:
            raise StatisticalValidationEligibilityError(
                "Every CSCV partition must contain at least four observations."
            )
        split_index = int(
            Decimal(self._matrix.observation_count)
            * self._policy.ranking_split_fraction
        )
        if split_index < 4 or self._matrix.observation_count - split_index < 4:
            raise StatisticalValidationEligibilityError(
                "Ranking split must leave at least four observations on each side."
            )
        if self._registry_audit is not None:
            if self._registry_audit.search_id != self._search_id:
                raise StatisticalValidationConfigurationError(
                    "Registry audit search ID does not match validation input."
                )
            if (
                self._registry_audit.declared_total_trials
                != self._attempted_trial_count
            ):
                raise StatisticalValidationConfigurationError(
                    "Registry declared trial count does not match attempted trials."
                )


def _base_trial_statistics(
    trial_id: str,
    returns: tuple[Decimal, ...],
    *,
    annualization_periods: int,
) -> TrialStatistics:
    values = tuple(float(value) for value in returns)
    mean = statistics.fmean(values)
    standard_deviation = statistics.stdev(values)
    if standard_deviation <= 0 or not math.isfinite(standard_deviation):
        raise StatisticalValidationEligibilityError(
            f"Trial {trial_id} has undefined Sharpe ratio due to zero variance."
        )
    period_sharpe = mean / standard_deviation
    skewness, kurtosis = _adjusted_skewness_kurtosis(values)
    probability = _probabilistic_sharpe_probability(
        sharpe_ratio=period_sharpe,
        benchmark_sharpe=0.0,
        observation_count=len(values),
        skewness=skewness,
        kurtosis=kurtosis,
    )
    decimal_probability = _probability_decimal(probability)
    return TrialStatistics(
        trial_id=trial_id,
        observation_count=len(values),
        mean_return=sum(returns, Decimal("0")) / len(returns),
        standard_deviation=standard_deviation,
        period_sharpe_ratio=period_sharpe,
        annualized_sharpe_ratio=period_sharpe * math.sqrt(annualization_periods),
        skewness=skewness,
        kurtosis=kurtosis,
        probabilistic_sharpe_probability=decimal_probability,
        one_sided_p_value=_probability_decimal(1.0 - probability),
        bonferroni_passed=False,
        false_discovery_q_value=Decimal("1"),
        false_discovery_passed=False,
    )


def _deflated_sharpe_result(
    *,
    trial_statistics: tuple[TrialStatistics, ...],
    selected_trial_id: str,
    attempted_trial_count: int,
    effective_trial_count: int,
    policy: StatisticalValidationPolicy,
) -> DeflatedSharpeResult:
    selected = next(
        item for item in trial_statistics if item.trial_id == selected_trial_id
    )
    sharpe_values = tuple(item.period_sharpe_ratio for item in trial_statistics)
    sharpe_variance = statistics.variance(sharpe_values)
    expected_maximum = _expected_maximum_sharpe(
        sharpe_variance=sharpe_variance,
        effective_trial_count=effective_trial_count,
    )
    probability = _probabilistic_sharpe_probability(
        sharpe_ratio=selected.period_sharpe_ratio,
        benchmark_sharpe=expected_maximum,
        observation_count=selected.observation_count,
        skewness=selected.skewness,
        kurtosis=selected.kurtosis,
    )
    decimal_probability = _probability_decimal(probability)
    return DeflatedSharpeResult(
        selected_trial_id=selected_trial_id,
        attempted_trial_count=attempted_trial_count,
        successful_trial_count=len(trial_statistics),
        effective_trial_count=effective_trial_count,
        sharpe_variance=sharpe_variance,
        expected_maximum_period_sharpe=expected_maximum,
        selected_period_sharpe=selected.period_sharpe_ratio,
        deflated_sharpe_probability=decimal_probability,
        passed=(
            decimal_probability >= policy.minimum_deflated_sharpe_probability
        ),
    )


def _probability_backtest_overfitting(
    *,
    matrix: StatisticalReturnsMatrix,
    policy: StatisticalValidationPolicy,
) -> ProbabilityBacktestOverfittingResult:
    partition_count = policy.cscv_partitions
    partition_size = matrix.observation_count // partition_count
    partitions = tuple(
        tuple(range(index * partition_size, (index + 1) * partition_size))
        for index in range(partition_count)
    )
    combinations = tuple(
        itertools.combinations(range(partition_count), partition_count // 2)
    )
    splits: list[CscvSplitResult] = []
    selected_is_scores: list[float] = []
    selected_oos_scores: list[float] = []
    all_partition_ids = set(range(partition_count))
    for split_number, in_partition_ids in enumerate(combinations, start=1):
        out_partition_ids = tuple(
            sorted(all_partition_ids - set(in_partition_ids))
        )
        in_indexes = tuple(
            index
            for partition_id in in_partition_ids
            for index in partitions[partition_id]
        )
        out_indexes = tuple(
            index
            for partition_id in out_partition_ids
            for index in partitions[partition_id]
        )
        in_scores = {
            trial_id: _subset_sharpe(series, in_indexes, trial_id)
            for trial_id, series in zip(
                matrix.trial_ids,
                matrix.returns_by_trial,
                strict=True,
            )
        }
        selected_trial_id = max(
            matrix.trial_ids,
            key=lambda trial_id: (in_scores[trial_id], trial_id),
        )
        out_scores = {
            trial_id: _subset_sharpe(series, out_indexes, trial_id)
            for trial_id, series in zip(
                matrix.trial_ids,
                matrix.returns_by_trial,
                strict=True,
            )
        }
        ranks = _average_ranks(out_scores)
        selected_rank = ranks[selected_trial_id]
        relative_rank = selected_rank / (len(matrix.trial_ids) + 1)
        logit = math.log(relative_rank / (1.0 - relative_rank))
        selected_in = in_scores[selected_trial_id]
        selected_out = out_scores[selected_trial_id]
        selected_is_scores.append(selected_in)
        selected_oos_scores.append(selected_out)
        splits.append(
            CscvSplitResult(
                split_number=split_number,
                in_sample_partitions=tuple(in_partition_ids),
                selected_trial_id=selected_trial_id,
                in_sample_sharpe=selected_in,
                out_of_sample_sharpe=selected_out,
                out_of_sample_rank=selected_rank,
                relative_rank=relative_rank,
                logit=logit,
                out_of_sample_loss=selected_out < 0,
            )
        )
    pbo = Decimal(sum(1 for split in splits if split.logit <= 0)) / len(splits)
    probability_loss = Decimal(
        sum(1 for split in splits if split.out_of_sample_loss)
    ) / len(splits)
    return ProbabilityBacktestOverfittingResult(
        partition_count=partition_count,
        combination_count=len(splits),
        probability_backtest_overfitting=pbo,
        probability_out_of_sample_loss=probability_loss,
        median_logit=statistics.median(split.logit for split in splits),
        performance_degradation_slope=_regression_slope(
            selected_is_scores,
            selected_oos_scores,
        ),
        passed=pbo <= policy.maximum_probability_backtest_overfitting,
        splits=tuple(splits),
    )


def _rank_stability(
    *,
    matrix: StatisticalReturnsMatrix,
    policy: StatisticalValidationPolicy,
) -> RankStabilityResult:
    split_index = int(
        Decimal(matrix.observation_count) * policy.ranking_split_fraction
    )
    in_indexes = tuple(range(split_index))
    out_indexes = tuple(range(split_index, matrix.observation_count))
    in_scores = {
        trial_id: _subset_sharpe(series, in_indexes, trial_id)
        for trial_id, series in zip(
            matrix.trial_ids,
            matrix.returns_by_trial,
            strict=True,
        )
    }
    out_scores = {
        trial_id: _subset_sharpe(series, out_indexes, trial_id)
        for trial_id, series in zip(
            matrix.trial_ids,
            matrix.returns_by_trial,
            strict=True,
        )
    }
    in_ranks = _average_ranks(in_scores)
    out_ranks = _average_ranks(out_scores)
    selected_trial_id = max(
        matrix.trial_ids,
        key=lambda trial_id: (in_scores[trial_id], trial_id),
    )
    selected_rank = out_ranks[selected_trial_id]
    relative_rank = selected_rank / (len(matrix.trial_ids) + 1)
    correlation = _pearson_correlation(
        tuple(in_ranks[trial_id] for trial_id in matrix.trial_ids),
        tuple(out_ranks[trial_id] for trial_id in matrix.trial_ids),
    )
    rank_reversal = relative_rank <= 0.5
    return RankStabilityResult(
        selected_in_sample_trial_id=selected_trial_id,
        selected_out_of_sample_rank=selected_rank,
        selected_out_of_sample_relative_rank=relative_rank,
        spearman_rank_correlation=correlation,
        rank_reversal=rank_reversal,
        passed=(
            not rank_reversal
            and correlation >= policy.minimum_rank_correlation
        ),
    )


def _adjusted_skewness_kurtosis(values: Sequence[float]) -> tuple[float, float]:
    observation_count = len(values)
    if observation_count < 4:
        raise StatisticalValidationEligibilityError(
            "Skewness and kurtosis require at least four observations."
        )
    mean = statistics.fmean(values)
    centered = tuple(value - mean for value in values)
    second_moment = statistics.fmean(value**2 for value in centered)
    if second_moment <= 0:
        raise StatisticalValidationEligibilityError(
            "Skewness and kurtosis are undefined for zero-variance returns."
        )
    third_moment = statistics.fmean(value**3 for value in centered)
    fourth_moment = statistics.fmean(value**4 for value in centered)
    biased_skewness = third_moment / second_moment**1.5
    adjusted_skewness = (
        math.sqrt(observation_count * (observation_count - 1))
        / (observation_count - 2)
        * biased_skewness
    )
    biased_excess_kurtosis = fourth_moment / second_moment**2 - 3.0
    adjusted_excess_kurtosis = (
        (observation_count - 1)
        / ((observation_count - 2) * (observation_count - 3))
        * ((observation_count + 1) * biased_excess_kurtosis + 6.0)
    )
    return adjusted_skewness, adjusted_excess_kurtosis + 3.0


def _probabilistic_sharpe_probability(
    *,
    sharpe_ratio: float,
    benchmark_sharpe: float,
    observation_count: int,
    skewness: float,
    kurtosis: float,
) -> float:
    denominator_squared = (
        1.0
        - skewness * sharpe_ratio
        + ((kurtosis - 1.0) / 4.0) * sharpe_ratio**2
    )
    if denominator_squared <= 0 or not math.isfinite(denominator_squared):
        raise StatisticalValidationEligibilityError(
            "Probabilistic Sharpe denominator is not positive and finite."
        )
    statistic = (
        (sharpe_ratio - benchmark_sharpe)
        * math.sqrt(observation_count - 1)
        / math.sqrt(denominator_squared)
    )
    return _NORMAL.cdf(statistic)


def _expected_maximum_sharpe(
    *,
    sharpe_variance: float,
    effective_trial_count: int,
) -> float:
    if effective_trial_count <= 1 or sharpe_variance <= 0:
        return 0.0
    standard_deviation = math.sqrt(sharpe_variance)
    count = float(effective_trial_count)
    first_quantile = _NORMAL.inv_cdf(1.0 - 1.0 / count)
    second_quantile = _NORMAL.inv_cdf(1.0 - 1.0 / (count * math.e))
    return standard_deviation * (
        (1.0 - _EULER_MASCHERONI) * first_quantile
        + _EULER_MASCHERONI * second_quantile
    )


def _benjamini_hochberg_q_values(
    p_values: dict[str, Decimal],
) -> dict[str, Decimal]:
    ordered = sorted(p_values.items(), key=lambda item: (item[1], item[0]))
    count = len(ordered)
    raw = [
        min(Decimal("1"), p_value * Decimal(count) / rank)
        for rank, (_, p_value) in enumerate(ordered, start=1)
    ]
    adjusted = list(raw)
    running = Decimal("1")
    for index in range(count - 1, -1, -1):
        running = min(running, adjusted[index])
        adjusted[index] = running
    return {
        trial_id: adjusted[index]
        for index, (trial_id, _) in enumerate(ordered)
    }


def _subset_sharpe(
    series: tuple[Decimal, ...],
    indexes: tuple[int, ...],
    trial_id: str,
) -> float:
    values = tuple(float(series[index]) for index in indexes)
    standard_deviation = statistics.stdev(values)
    if standard_deviation <= 0 or not math.isfinite(standard_deviation):
        raise StatisticalValidationEligibilityError(
            f"Trial {trial_id} has zero variance in a validation subset."
        )
    return statistics.fmean(values) / standard_deviation


def _average_ranks(scores: dict[str, float]) -> dict[str, float]:
    ordered = sorted(scores.items(), key=lambda item: (item[1], item[0]))
    ranks: dict[str, float] = {}
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and math.isclose(
            ordered[end][1],
            ordered[index][1],
            rel_tol=1e-12,
            abs_tol=1e-15,
        ):
            end += 1
        average_rank = ((index + 1) + end) / 2.0
        for position in range(index, end):
            ranks[ordered[position][0]] = average_rank
        index = end
    return ranks


def _pearson_correlation(first: Sequence[float], second: Sequence[float]) -> float:
    if len(first) != len(second) or len(first) < 2:
        raise StatisticalValidationEligibilityError(
            "Rank correlation requires two equally sized sequences."
        )
    first_mean = statistics.fmean(first)
    second_mean = statistics.fmean(second)
    first_centered = tuple(value - first_mean for value in first)
    second_centered = tuple(value - second_mean for value in second)
    denominator = math.sqrt(
        sum(value**2 for value in first_centered)
        * sum(value**2 for value in second_centered)
    )
    if denominator == 0:
        return 0.0
    return sum(
        first_value * second_value
        for first_value, second_value in zip(
            first_centered,
            second_centered,
            strict=True,
        )
    ) / denominator


def _regression_slope(first: Sequence[float], second: Sequence[float]) -> float | None:
    if len(first) != len(second) or not first:
        return None
    first_mean = statistics.fmean(first)
    second_mean = statistics.fmean(second)
    denominator = sum((value - first_mean) ** 2 for value in first)
    if denominator == 0:
        return None
    return sum(
        (first_value - first_mean) * (second_value - second_mean)
        for first_value, second_value in zip(first, second, strict=True)
    ) / denominator


def _warnings(
    *,
    attempted_trial_count: int,
    effective_trial_count: int,
    successful_trial_count: int,
    failed_trial_count: int,
    registry_audit: StatisticalRegistryAudit | None,
) -> tuple[str, ...]:
    warnings: list[str] = []
    omitted = attempted_trial_count - successful_trial_count - failed_trial_count
    if omitted > 0:
        warnings.append(
            f"{omitted} declared trials have neither returns nor recorded failures."
        )
    if effective_trial_count < attempted_trial_count:
        warnings.append(
            "Effective trial count is below attempted trials; the independence "
            "assumption must be documented externally."
        )
    if registry_audit is None:
        warnings.append(
            "Experiment registry was not supplied; trial completeness is self-declared."
        )
    elif not registry_audit.complete:
        warnings.append(
            "Experiment registry search is incomplete or contains pending outcomes."
        )
    return tuple(warnings)


def _parse_timestamp(raw_value: str, line_number: int) -> datetime:
    value = raw_value.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise StatisticalValidationConfigurationError(
            f"Returns CSV line {line_number} has an invalid timestamp."
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StatisticalValidationConfigurationError(
            f"Returns CSV line {line_number} timestamp must be timezone-aware."
        )
    return parsed.astimezone(UTC)


def _parse_return(raw_value: str, line_number: int, trial_id: str) -> Decimal:
    value = raw_value.strip()
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise StatisticalValidationConfigurationError(
            f"Returns CSV line {line_number} has an invalid return for {trial_id}."
        ) from error
    if not parsed.is_finite() or parsed <= Decimal("-1"):
        raise StatisticalValidationConfigurationError(
            f"Returns CSV line {line_number} has an invalid return for {trial_id}."
        )
    return parsed


def _probability_decimal(value: float) -> Decimal:
    clipped = min(1.0, max(0.0, value))
    return Decimal(format(clipped, ".15g"))


def _validate_digest(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise StatisticalValidationConfigurationError(
            f"{field_name} must be a lowercase SHA-256 digest."
        )
