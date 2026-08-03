from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from world_quant_system.research.portfolio_promotion_models import (
    AllocationMethod,
    AllocationSnapshot,
    CandidateDecision,
    CandidateEvidence,
    CandidateGateResult,
    CandidateReturnMatrix,
    CandidateWeight,
    CorrelationPair,
    PortfolioFoldResult,
    PortfolioMetrics,
    PortfolioPromotionConfigurationError,
    PortfolioPromotionEligibilityError,
    PortfolioPromotionIntegrityError,
    PortfolioPromotionReport,
    PortfolioScenarioResult,
    PortfolioWalkForwardResult,
    PromotionDecision,
    PromotionPolicy,
    RedundancyCluster,
    StressScenarioName,
    canonical_json_bytes,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_BPS = Decimal("10000")
_WEIGHT_EPSILON = Decimal("0.000000000001")
_MAX_INPUT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _ScenarioComputation:
    result: PortfolioScenarioResult
    snapshots: tuple[AllocationSnapshot, ...]
    periodic_returns: tuple[Decimal, ...]
    per_period_cash: tuple[Decimal, ...]
    costs: tuple[Decimal, ...]


class DeterministicPortfolioPromotionEngine:
    """Evaluate validated strategy candidates as a research-only portfolio."""

    def __init__(
        self,
        *,
        candidate_evidence: Sequence[CandidateEvidence],
        return_matrix: CandidateReturnMatrix,
        allocation_method: AllocationMethod,
        policy: PromotionPolicy | None = None,
    ) -> None:
        evidence = tuple(sorted(candidate_evidence, key=lambda item: item.candidate_id))
        if not evidence:
            raise PortfolioPromotionEligibilityError(
                "Portfolio promotion requires candidate evidence."
            )
        if len({item.candidate_id for item in evidence}) != len(evidence):
            raise PortfolioPromotionConfigurationError(
                "Candidate evidence IDs must be unique."
            )
        evidence_ids = tuple(item.candidate_id for item in evidence)
        if evidence_ids != return_matrix.candidate_ids:
            raise PortfolioPromotionIntegrityError(
                "Candidate evidence IDs must exactly match return-matrix columns."
            )
        dataset_digests = {item.dataset_digest for item in evidence}
        if len(dataset_digests) != 1:
            raise PortfolioPromotionIntegrityError(
                "All candidate evidence must use one frozen dataset digest."
            )
        if not isinstance(allocation_method, AllocationMethod):
            raise PortfolioPromotionConfigurationError(
                "Allocation method must be an AllocationMethod."
            )
        self._evidence = evidence
        self._matrix = return_matrix
        self._allocation_method = allocation_method
        self._policy = policy or PromotionPolicy()
        self._has_run = False

    def run(
        self,
        *,
        created_at: datetime | None = None,
    ) -> PortfolioPromotionReport:
        if self._has_run:
            raise PortfolioPromotionConfigurationError(
                "Portfolio-promotion engines are single-use."
            )
        self._has_run = True
        candidate_results = tuple(
            self._evaluate_candidate(item) for item in self._evidence
        )
        eligible_ids = tuple(
            item.candidate_id
            for item in candidate_results
            if item.decision is CandidateDecision.ELIGIBLE
        )
        correlations = self._build_correlations(eligible_ids)
        clusters = _build_clusters(
            eligible_ids,
            correlations,
            self._policy,
        )
        scenarios, snapshots, walk_forward = self._build_scenarios(
            eligible_ids,
            clusters,
        )
        decision, decision_reasons = self._decision(
            candidate_results,
            eligible_ids,
            scenarios,
            walk_forward,
        )
        timestamp = created_at or datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise PortfolioPromotionConfigurationError(
                "Report creation timestamp must be timezone-aware."
            )
        return PortfolioPromotionReport(
            dataset_digest=self._evidence[0].dataset_digest,
            matrix_digest=self._matrix.matrix_digest,
            allocation_method=self._allocation_method,
            policy=self._policy,
            candidate_evidence=self._evidence,
            candidate_results=candidate_results,
            correlations=correlations,
            redundancy_clusters=clusters,
            allocation_snapshots=snapshots,
            walk_forward=walk_forward,
            scenarios=scenarios,
            decision=decision,
            decision_reasons=decision_reasons,
            created_at=timestamp.astimezone(UTC),
        )

    def _evaluate_candidate(
        self,
        evidence: CandidateEvidence,
    ) -> CandidateGateResult:
        returns = self._matrix.series_for(evidence.candidate_id)
        cumulative_return = _cumulative_return(returns)
        maximum_drawdown = _maximum_drawdown(returns)
        rejected_reasons: list[str] = []
        insufficient_reasons: list[str] = []
        if not evidence.robustness_passed:
            rejected_reasons.append("robustness validation did not pass")
        if not evidence.statistical_passed:
            rejected_reasons.append("statistical validation did not pass")
        if len(returns) < self._policy.minimum_candidate_observations:
            insufficient_reasons.append("candidate observations below policy minimum")
        if cumulative_return < self._policy.minimum_candidate_cumulative_return:
            rejected_reasons.append("candidate cumulative return below policy minimum")
        if maximum_drawdown < self._policy.maximum_candidate_drawdown:
            rejected_reasons.append("candidate drawdown exceeds policy maximum")
        if evidence.declared_turnover is None:
            insufficient_reasons.append("declared turnover is missing")
        elif evidence.declared_turnover > self._policy.maximum_declared_turnover:
            rejected_reasons.append("declared turnover exceeds policy maximum")
        if evidence.declared_cost_to_gross_profit_ratio is None:
            insufficient_reasons.append("cost-to-gross-profit ratio is missing")
        elif (
            evidence.declared_cost_to_gross_profit_ratio
            > self._policy.maximum_cost_to_gross_profit_ratio
        ):
            rejected_reasons.append(
                "cost-to-gross-profit ratio exceeds policy maximum"
            )
        if rejected_reasons:
            decision = CandidateDecision.REJECTED
            reasons = tuple(rejected_reasons + insufficient_reasons)
        elif insufficient_reasons:
            decision = CandidateDecision.INSUFFICIENT_EVIDENCE
            reasons = tuple(insufficient_reasons)
        else:
            decision = CandidateDecision.ELIGIBLE
            reasons = ()
        return CandidateGateResult(
            candidate_id=evidence.candidate_id,
            decision=decision,
            reasons=reasons,
            observation_count=len(returns),
            cumulative_return=cumulative_return,
            maximum_drawdown=maximum_drawdown,
        )

    def _build_correlations(
        self,
        eligible_ids: tuple[str, ...],
    ) -> tuple[CorrelationPair, ...]:
        pairs: list[CorrelationPair] = []
        for left_id, right_id in itertools.combinations(eligible_ids, 2):
            left = self._matrix.series_for(left_id)
            right = self._matrix.series_for(right_id)
            loss_indexes = tuple(
                index
                for index, (left_value, right_value) in enumerate(
                    zip(left, right, strict=True)
                )
                if left_value < _ZERO or right_value < _ZERO
            )
            loss_correlation: float | None = None
            if len(loss_indexes) >= self._policy.minimum_loss_period_observations:
                loss_correlation = _pearson(
                    tuple(float(left[index]) for index in loss_indexes),
                    tuple(float(right[index]) for index in loss_indexes),
                )
            pairs.append(
                CorrelationPair(
                    left_candidate_id=left_id,
                    right_candidate_id=right_id,
                    pearson_correlation=_pearson(
                        tuple(float(value) for value in left),
                        tuple(float(value) for value in right),
                    ),
                    spearman_correlation=_spearman(
                        tuple(float(value) for value in left),
                        tuple(float(value) for value in right),
                    ),
                    loss_period_correlation=loss_correlation,
                    loss_period_observations=len(loss_indexes),
                )
            )
        return tuple(pairs)

    def _build_scenarios(
        self,
        eligible_ids: tuple[str, ...],
        clusters: tuple[RedundancyCluster, ...],
    ) -> tuple[
        tuple[PortfolioScenarioResult, ...],
        tuple[AllocationSnapshot, ...],
        PortfolioWalkForwardResult,
    ]:
        base = self._run_scenario(
            eligible_ids=eligible_ids,
            clusters=clusters,
            scenario=StressScenarioName.BASE,
            cost_bps=self._policy.base_cost_bps,
            common_loss_multiplier=_ONE,
            removed_candidate_id=None,
        )
        adverse = self._run_scenario(
            eligible_ids=eligible_ids,
            clusters=clusters,
            scenario=StressScenarioName.ADVERSE_COST,
            cost_bps=self._policy.adverse_cost_bps,
            common_loss_multiplier=_ONE,
            removed_candidate_id=None,
        )
        severe = self._run_scenario(
            eligible_ids=eligible_ids,
            clusters=clusters,
            scenario=StressScenarioName.SEVERE_COST,
            cost_bps=self._policy.severe_cost_bps,
            common_loss_multiplier=_ONE,
            removed_candidate_id=None,
        )
        common_loss = self._run_scenario(
            eligible_ids=eligible_ids,
            clusters=clusters,
            scenario=StressScenarioName.COMMON_LOSS_SHOCK,
            cost_bps=self._policy.adverse_cost_bps,
            common_loss_multiplier=self._policy.common_loss_multiplier,
            removed_candidate_id=None,
        )
        largest_candidate = _largest_average_weight_candidate(base.snapshots)
        reduced_ids = tuple(
            candidate_id
            for candidate_id in eligible_ids
            if candidate_id != largest_candidate
        )
        reduced_clusters = _restrict_clusters(clusters, reduced_ids)
        removal = self._run_scenario(
            eligible_ids=reduced_ids,
            clusters=reduced_clusters,
            scenario=StressScenarioName.LARGEST_CANDIDATE_REMOVED,
            cost_bps=self._policy.adverse_cost_bps,
            common_loss_multiplier=_ONE,
            removed_candidate_id=largest_candidate,
        )
        results = (
            base.result,
            adverse.result,
            severe.result,
            common_loss.result,
            removal.result,
        )
        walk_forward = _walk_forward_result(
            matrix=self._matrix,
            base=base,
            policy=self._policy,
        )
        return results, base.snapshots, walk_forward

    def _run_scenario(
        self,
        *,
        eligible_ids: tuple[str, ...],
        clusters: tuple[RedundancyCluster, ...],
        scenario: StressScenarioName,
        cost_bps: Decimal,
        common_loss_multiplier: Decimal,
        removed_candidate_id: str | None,
    ) -> _ScenarioComputation:
        periodic_returns, snapshots, per_period_cash, costs = _portfolio_returns(
            matrix=self._matrix,
            candidate_ids=eligible_ids,
            clusters=clusters,
            allocation_method=self._allocation_method,
            policy=self._policy,
            cost_bps=cost_bps,
            common_loss_multiplier=common_loss_multiplier,
        )
        metrics = _portfolio_metrics(
            periodic_returns,
            snapshots=snapshots,
            per_period_cash=per_period_cash,
            costs=costs,
            policy=self._policy,
        )
        reasons = _scenario_reasons(scenario, metrics, self._policy)
        result = PortfolioScenarioResult(
            scenario=scenario,
            cost_bps=cost_bps,
            common_loss_multiplier=common_loss_multiplier,
            removed_candidate_id=removed_candidate_id,
            metrics=metrics,
            passed=not reasons,
            reasons=reasons,
            periodic_returns_digest=hashlib.sha256(
                canonical_json_bytes([format(value, "f") for value in periodic_returns])
            ).hexdigest(),
        )
        return _ScenarioComputation(
            result=result,
            snapshots=snapshots,
            periodic_returns=periodic_returns,
            per_period_cash=per_period_cash,
            costs=costs,
        )

    def _decision(
        self,
        candidate_results: tuple[CandidateGateResult, ...],
        eligible_ids: tuple[str, ...],
        scenarios: tuple[PortfolioScenarioResult, ...],
        walk_forward: PortfolioWalkForwardResult,
    ) -> tuple[PromotionDecision, tuple[str, ...]]:
        insufficient_count = sum(
            item.decision is CandidateDecision.INSUFFICIENT_EVIDENCE
            for item in candidate_results
        )
        if len(eligible_ids) < self._policy.minimum_eligible_candidates:
            reasons = ["eligible candidate count below policy minimum"]
            if insufficient_count:
                reasons.append("one or more candidates have insufficient evidence")
            return PromotionDecision.INSUFFICIENT_EVIDENCE, tuple(reasons)
        failed = tuple(item for item in scenarios if not item.passed)
        reasons = [
            f"{item.scenario.value}: {reason}"
            for item in failed
            for reason in item.reasons
        ]
        if not walk_forward.passed:
            reasons.append("portfolio walk-forward pass rate below policy minimum")
        if reasons:
            return PromotionDecision.REJECTED, tuple(reasons)
        return PromotionDecision.PROMOTED_FOR_SHADOW_RESEARCH, ()


def parse_candidate_returns_csv(path: Path | str) -> CandidateReturnMatrix:
    source = _require_local_file(path, "Candidate returns CSV")
    try:
        with source.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration as error:
                raise PortfolioPromotionConfigurationError(
                    "Candidate returns CSV is empty."
                ) from error
            if len(header) < 3 or header[0].strip().lower() != "timestamp":
                raise PortfolioPromotionConfigurationError(
                    "Returns CSV must use timestamp followed by at least "
                    "two candidates."
                )
            raw_ids = tuple(value.strip() for value in header[1:])
            if any(not candidate_id for candidate_id in raw_ids):
                raise PortfolioPromotionConfigurationError(
                    "Candidate return IDs must be nonblank."
                )
            if len(set(raw_ids)) != len(raw_ids):
                raise PortfolioPromotionConfigurationError(
                    "Candidate return IDs must be unique."
                )
            sorted_ids = tuple(sorted(raw_ids))
            source_indexes = tuple(
                raw_ids.index(candidate_id) for candidate_id in sorted_ids
            )
            timestamps: list[datetime] = []
            columns: list[list[Decimal]] = [[] for _ in sorted_ids]
            expected_width = len(header)
            for line_number, row in enumerate(reader, start=2):
                if not row or all(not value.strip() for value in row):
                    raise PortfolioPromotionConfigurationError(
                        f"Returns CSV contains a blank row at line {line_number}."
                    )
                if len(row) != expected_width:
                    raise PortfolioPromotionConfigurationError(
                        f"Returns CSV line {line_number} has the wrong column count."
                    )
                timestamp = _parse_timestamp(row[0], line_number)
                if timestamps and timestamp <= timestamps[-1]:
                    raise PortfolioPromotionConfigurationError(
                        "Return timestamps must be strictly increasing."
                    )
                timestamps.append(timestamp)
                for target_index, source_index in enumerate(source_indexes):
                    columns[target_index].append(
                        _parse_return(
                            row[source_index + 1],
                            line_number,
                            sorted_ids[target_index],
                        )
                    )
    except UnicodeDecodeError as error:
        raise PortfolioPromotionConfigurationError(
            "Candidate returns CSV must be UTF-8 encoded."
        ) from error
    return CandidateReturnMatrix(
        timestamps=tuple(timestamps),
        candidate_ids=sorted_ids,
        returns_by_candidate=tuple(tuple(column) for column in columns),
    )


def load_candidate_evidence_manifest(
    path: Path | str,
) -> tuple[CandidateEvidence, ...]:
    manifest_path = _require_local_file(path, "Candidate evidence manifest")
    document = _load_json(manifest_path, "Candidate evidence manifest")
    if document.get("schema_version") != 1:
        raise PortfolioPromotionConfigurationError(
            "Candidate evidence manifest schema_version must be 1."
        )
    dataset_digest = _required_sha256(document, "dataset_digest")
    raw_candidates = document.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise PortfolioPromotionConfigurationError(
            "Candidate evidence manifest must contain a nonempty candidates list."
        )
    evidence: list[CandidateEvidence] = []
    for index, raw_candidate in enumerate(raw_candidates):
        if not isinstance(raw_candidate, dict):
            raise PortfolioPromotionConfigurationError(
                f"Candidate manifest item {index} must be an object."
            )
        candidate = cast(dict[str, object], raw_candidate)
        candidate_id = _required_string(candidate, "candidate_id")
        backtest_path = _resolve_manifest_path(
            manifest_path,
            _required_string(candidate, "backtest_report"),
        )
        robustness_path = _resolve_manifest_path(
            manifest_path,
            _required_string(candidate, "robustness_report"),
        )
        statistical_path = _resolve_manifest_path(
            manifest_path,
            _required_string(candidate, "statistical_report"),
        )
        backtest_sha256 = _verified_manifest_file_sha256(
            candidate,
            "backtest_report_sha256",
            backtest_path,
            candidate_id,
        )
        robustness_sha256 = _verified_manifest_file_sha256(
            candidate,
            "robustness_report_sha256",
            robustness_path,
            candidate_id,
        )
        statistical_sha256 = _verified_manifest_file_sha256(
            candidate,
            "statistical_report_sha256",
            statistical_path,
            candidate_id,
        )
        backtest_document = _load_json(backtest_path, "Backtest report")
        robustness_document = _load_json(robustness_path, "Robustness report")
        statistical_document = _load_json(
            statistical_path,
            "Statistical-validation report",
        )
        for report_document, report_name in (
            (backtest_document, "Backtest report"),
            (robustness_document, "Robustness report"),
            (statistical_document, "Statistical-validation report"),
        ):
            _require_dataset_binding(
                report_document,
                dataset_digest,
                candidate_id,
                report_name,
            )
        declared_turnover = _optional_decimal(candidate, "declared_turnover")
        reported_turnover = _nested_optional_decimal(
            backtest_document,
            ("metrics", "turnover"),
        )
        declared_turnover = _reconcile_economic_evidence(
            declared=declared_turnover,
            reported=reported_turnover,
            candidate_id=candidate_id,
            field_name="turnover",
        )
        declared_cost_ratio = _optional_decimal(
            candidate,
            "declared_cost_to_gross_profit_ratio",
        )
        reported_cost_ratio = _backtest_cost_to_gross_profit_ratio(
            backtest_document
        )
        declared_cost_ratio = _reconcile_economic_evidence(
            declared=declared_cost_ratio,
            reported=reported_cost_ratio,
            candidate_id=candidate_id,
            field_name="cost-to-gross-profit ratio",
        )
        evidence.append(
            CandidateEvidence(
                candidate_id=candidate_id,
                strategy_name=_required_string(candidate, "strategy_name"),
                strategy_version=_required_string(candidate, "strategy_version"),
                dataset_digest=dataset_digest,
                backtest_file_sha256=backtest_sha256,
                robustness_file_sha256=robustness_sha256,
                robustness_report_digest=_required_sha256(
                    robustness_document,
                    "report_digest",
                ),
                robustness_passed=_required_bool(
                    robustness_document,
                    "passed",
                ),
                statistical_file_sha256=statistical_sha256,
                statistical_report_digest=_required_sha256(
                    statistical_document,
                    "report_digest",
                ),
                statistical_passed=_required_bool(
                    statistical_document,
                    "passed",
                ),
                declared_turnover=declared_turnover,
                declared_cost_to_gross_profit_ratio=declared_cost_ratio,
            )
        )
    result = tuple(sorted(evidence, key=lambda item: item.candidate_id))
    if len({item.candidate_id for item in result}) != len(result):
        raise PortfolioPromotionConfigurationError(
            "Candidate manifest IDs must be unique."
        )
    return result


def _portfolio_returns(
    *,
    matrix: CandidateReturnMatrix,
    candidate_ids: tuple[str, ...],
    clusters: tuple[RedundancyCluster, ...],
    allocation_method: AllocationMethod,
    policy: PromotionPolicy,
    cost_bps: Decimal,
    common_loss_multiplier: Decimal,
) -> tuple[
    tuple[Decimal, ...],
    tuple[AllocationSnapshot, ...],
    tuple[Decimal, ...],
    tuple[Decimal, ...],
]:
    current_weights = {candidate_id: _ZERO for candidate_id in candidate_ids}
    periodic_returns: list[Decimal] = []
    snapshots: list[AllocationSnapshot] = []
    cash_weights: list[Decimal] = []
    costs: list[Decimal] = []
    for index, timestamp in enumerate(matrix.timestamps):
        should_rebalance = _should_rebalance(
            index,
            allocation_method,
            policy,
        )
        period_cost = _ZERO
        if should_rebalance:
            raw_scores = _raw_allocation_scores(
                matrix=matrix,
                candidate_ids=candidate_ids,
                allocation_method=allocation_method,
                lookback_end=index,
                policy=policy,
            )
            target_weights = _constrained_weights(
                raw_scores,
                clusters=clusters,
                policy=policy,
            )
            turnover = sum(
                (
                    abs(
                        target_weights.get(candidate_id, _ZERO)
                        - current_weights.get(candidate_id, _ZERO)
                    )
                    for candidate_id in sorted(
                        set(current_weights) | set(target_weights)
                    )
                ),
                _ZERO,
            )
            period_cost = turnover * cost_bps / _BPS
            current_weights = target_weights
            cash_weight = _ONE - sum(current_weights.values(), _ZERO)
            snapshots.append(
                AllocationSnapshot(
                    timestamp=timestamp,
                    weights=tuple(
                        CandidateWeight(candidate_id, current_weights[candidate_id])
                        for candidate_id in sorted(current_weights)
                    ),
                    cash_weight=cash_weight,
                    one_way_turnover=turnover,
                )
            )
        cash_weight = _ONE - sum(current_weights.values(), _ZERO)
        gross_return = _ZERO
        for candidate_id, weight in current_weights.items():
            candidate_return = matrix.series_for(candidate_id)[index]
            if candidate_return < _ZERO:
                candidate_return = max(
                    Decimal("-0.999999999999"),
                    candidate_return * common_loss_multiplier,
                )
            gross_return += weight * candidate_return
        net_return = max(
            Decimal("-0.999999999999"),
            gross_return - period_cost,
        )
        periodic_returns.append(net_return)
        cash_weights.append(cash_weight)
        costs.append(period_cost)
    return (
        tuple(periodic_returns),
        tuple(snapshots),
        tuple(cash_weights),
        tuple(costs),
    )


def _should_rebalance(
    index: int,
    allocation_method: AllocationMethod,
    policy: PromotionPolicy,
) -> bool:
    if allocation_method is AllocationMethod.EQUAL_WEIGHT:
        return index == 0 or index % policy.rebalance_frequency == 0
    if index < policy.volatility_lookback:
        return False
    return (index - policy.volatility_lookback) % policy.rebalance_frequency == 0


def _raw_allocation_scores(
    *,
    matrix: CandidateReturnMatrix,
    candidate_ids: tuple[str, ...],
    allocation_method: AllocationMethod,
    lookback_end: int,
    policy: PromotionPolicy,
) -> dict[str, Decimal]:
    if not candidate_ids:
        return {}
    if allocation_method is AllocationMethod.EQUAL_WEIGHT:
        return {candidate_id: _ONE for candidate_id in candidate_ids}
    if lookback_end < policy.volatility_lookback:
        return {}
    start = lookback_end - policy.volatility_lookback
    scores: dict[str, Decimal] = {}
    for candidate_id in candidate_ids:
        window = matrix.series_for(candidate_id)[start:lookback_end]
        standard_deviation = statistics.stdev(float(value) for value in window)
        if standard_deviation <= 0 or not math.isfinite(standard_deviation):
            scores[candidate_id] = _ZERO
        else:
            scores[candidate_id] = _ONE / Decimal(str(standard_deviation))
    if not any(value > _ZERO for value in scores.values()):
        return {candidate_id: _ONE for candidate_id in candidate_ids}
    return scores


def _constrained_weights(
    raw_scores: Mapping[str, Decimal],
    *,
    clusters: tuple[RedundancyCluster, ...],
    policy: PromotionPolicy,
) -> dict[str, Decimal]:
    candidate_ids = tuple(sorted(raw_scores))
    if not candidate_ids:
        return {}
    positive_scores = {
        candidate_id: max(_ZERO, raw_scores[candidate_id])
        for candidate_id in candidate_ids
    }
    if sum(positive_scores.values(), _ZERO) <= _ZERO:
        positive_scores = {candidate_id: _ONE for candidate_id in candidate_ids}
    cluster_for = _cluster_lookup(clusters, candidate_ids)
    weights = {candidate_id: _ZERO for candidate_id in candidate_ids}
    investable = _ONE - policy.minimum_cash_weight
    remaining = investable
    for _ in range(100):
        if remaining <= _WEIGHT_EPSILON:
            break
        active = tuple(
            candidate_id
            for candidate_id in candidate_ids
            if weights[candidate_id] + _WEIGHT_EPSILON
            < policy.maximum_candidate_weight
            and _cluster_weight(weights, cluster_for, cluster_for[candidate_id])
            + _WEIGHT_EPSILON
            < policy.maximum_cluster_weight
        )
        if not active:
            break
        active_score = sum((positive_scores[item] for item in active), _ZERO)
        if active_score <= _ZERO:
            break
        proposed = {
            candidate_id: remaining
            * positive_scores[candidate_id]
            / active_score
            for candidate_id in active
        }
        for cluster_id in sorted({cluster_for[item] for item in active}):
            members = tuple(
                item for item in active if cluster_for[item] == cluster_id
            )
            cluster_room = max(
                _ZERO,
                policy.maximum_cluster_weight
                - _cluster_weight(weights, cluster_for, cluster_id),
            )
            cluster_proposed = sum((proposed[item] for item in members), _ZERO)
            if cluster_proposed > cluster_room and cluster_proposed > _ZERO:
                scale = cluster_room / cluster_proposed
                for candidate_id in members:
                    proposed[candidate_id] *= scale
        increment_total = _ZERO
        for candidate_id in active:
            individual_room = max(
                _ZERO,
                policy.maximum_candidate_weight - weights[candidate_id],
            )
            increment = min(proposed[candidate_id], individual_room)
            weights[candidate_id] += increment
            increment_total += increment
        if increment_total <= _WEIGHT_EPSILON:
            break
        remaining -= increment_total
    return {
        candidate_id: weight
        for candidate_id, weight in weights.items()
        if weight > _WEIGHT_EPSILON
    }


def _portfolio_metrics(
    periodic_returns: tuple[Decimal, ...],
    *,
    snapshots: tuple[AllocationSnapshot, ...],
    per_period_cash: tuple[Decimal, ...],
    costs: tuple[Decimal, ...],
    policy: PromotionPolicy,
) -> PortfolioMetrics:
    count = len(periodic_returns)
    if count == 0:
        raise PortfolioPromotionEligibilityError(
            "Portfolio metrics require periodic returns."
        )
    values = tuple(float(value) for value in periodic_returns)
    cumulative = _cumulative_return(periodic_returns)
    final_equity = _ONE + cumulative
    annualized_return: float | None = None
    if final_equity > _ZERO:
        annualized_return = float(final_equity) ** (
            policy.annualization_periods / count
        ) - 1.0
    standard_deviation = statistics.stdev(values) if count >= 2 else 0.0
    annualized_volatility = standard_deviation * math.sqrt(
        policy.annualization_periods
    )
    sharpe: float | None = None
    if standard_deviation > 0:
        sharpe = (
            statistics.fmean(values)
            / standard_deviation
            * math.sqrt(policy.annualization_periods)
        )
    downside = tuple(min(0.0, value) for value in values)
    downside_deviation = math.sqrt(
        statistics.fmean(value * value for value in downside)
    )
    sortino: float | None = None
    if downside_deviation > 0:
        sortino = (
            statistics.fmean(values)
            / downside_deviation
            * math.sqrt(policy.annualization_periods)
        )
    expected_shortfall_count = max(
        1,
        math.ceil(count * float(policy.expected_shortfall_fraction)),
    )
    expected_shortfall = sum(
        sorted(periodic_returns)[:expected_shortfall_count],
        _ZERO,
    ) / Decimal(expected_shortfall_count)
    average_effective = _average_effective_strategy_count(snapshots)
    average_cash = sum(per_period_cash, _ZERO) / Decimal(len(per_period_cash))
    return PortfolioMetrics(
        observation_count=count,
        cumulative_return=cumulative,
        annualized_return=annualized_return,
        annualized_volatility=annualized_volatility,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        maximum_drawdown=_maximum_drawdown(periodic_returns),
        expected_shortfall=expected_shortfall,
        total_turnover=sum(
            (snapshot.one_way_turnover for snapshot in snapshots),
            _ZERO,
        ),
        total_cost=sum(costs, _ZERO),
        average_effective_strategy_count=average_effective,
        average_cash_weight=average_cash,
    )


def _walk_forward_result(
    *,
    matrix: CandidateReturnMatrix,
    base: _ScenarioComputation,
    policy: PromotionPolicy,
) -> PortfolioWalkForwardResult:
    observation_count = len(base.periodic_returns)
    if observation_count < (
        policy.walk_forward_folds * policy.minimum_fold_observations
    ):
        raise PortfolioPromotionEligibilityError(
            "Portfolio walk-forward folds do not have enough observations."
        )
    quotient, remainder = divmod(observation_count, policy.walk_forward_folds)
    fold_results: list[PortfolioFoldResult] = []
    start_index = 0
    for fold_index in range(policy.walk_forward_folds):
        fold_size = quotient + (1 if fold_index < remainder else 0)
        end_index = start_index + fold_size
        start_at = matrix.timestamps[start_index]
        end_at = matrix.timestamps[end_index - 1]
        fold_snapshots = tuple(
            snapshot
            for snapshot in base.snapshots
            if start_at <= snapshot.timestamp <= end_at
        )
        metrics = _portfolio_metrics(
            base.periodic_returns[start_index:end_index],
            snapshots=fold_snapshots,
            per_period_cash=base.per_period_cash[start_index:end_index],
            costs=base.costs[start_index:end_index],
            policy=policy,
        )
        reasons: list[str] = []
        if metrics.observation_count < policy.minimum_fold_observations:
            reasons.append("fold observations below policy minimum")
        if metrics.cumulative_return < policy.minimum_fold_cumulative_return:
            reasons.append("fold cumulative return below policy minimum")
        if metrics.maximum_drawdown < policy.maximum_fold_drawdown:
            reasons.append("fold drawdown exceeds policy maximum")
        fold_results.append(
            PortfolioFoldResult(
                fold_number=fold_index + 1,
                start_at=start_at,
                end_at=end_at,
                metrics=metrics,
                passed=not reasons,
                reasons=tuple(reasons),
            )
        )
        start_index = end_index
    passed_count = sum(item.passed for item in fold_results)
    pass_rate = Decimal(passed_count) / Decimal(len(fold_results))
    return PortfolioWalkForwardResult(
        fold_count=len(fold_results),
        pass_rate=pass_rate,
        worst_cumulative_return=min(
            item.metrics.cumulative_return for item in fold_results
        ),
        worst_maximum_drawdown=min(
            item.metrics.maximum_drawdown for item in fold_results
        ),
        passed=pass_rate >= policy.minimum_fold_pass_rate,
        folds=tuple(fold_results),
    )


def _scenario_reasons(
    scenario: StressScenarioName,
    metrics: PortfolioMetrics,
    policy: PromotionPolicy,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if scenario is StressScenarioName.BASE:
        if metrics.cumulative_return < policy.minimum_base_cumulative_return:
            reasons.append("base cumulative return below policy minimum")
        if (
            metrics.average_effective_strategy_count
            < policy.minimum_effective_strategy_count
        ):
            reasons.append("effective strategy count below policy minimum")
    elif scenario is StressScenarioName.ADVERSE_COST:
        if metrics.cumulative_return < policy.minimum_adverse_cumulative_return:
            reasons.append("adverse-cost return below policy minimum")
    elif scenario in (
        StressScenarioName.SEVERE_COST,
        StressScenarioName.COMMON_LOSS_SHOCK,
    ):
        if metrics.maximum_drawdown < policy.maximum_severe_drawdown:
            reasons.append("stress drawdown exceeds policy maximum")
    elif scenario is StressScenarioName.LARGEST_CANDIDATE_REMOVED:
        if metrics.cumulative_return < policy.minimum_adverse_cumulative_return:
            reasons.append("portfolio fails after largest candidate removal")
    return tuple(reasons)


def _build_clusters(
    candidate_ids: tuple[str, ...],
    correlations: tuple[CorrelationPair, ...],
    policy: PromotionPolicy,
) -> tuple[RedundancyCluster, ...]:
    parents = {candidate_id: candidate_id for candidate_id in candidate_ids}

    def find(candidate_id: str) -> str:
        parent = parents[candidate_id]
        while parent != parents[parent]:
            parents[parent] = parents[parents[parent]]
            parent = parents[parent]
        parents[candidate_id] = parent
        return parent

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        smaller, larger = sorted((left_root, right_root))
        parents[larger] = smaller

    for pair in correlations:
        high_normal = (
            pair.pearson_correlation >= policy.maximum_pairwise_correlation
        )
        high_loss = (
            pair.loss_period_correlation is not None
            and pair.loss_period_correlation
            >= policy.maximum_loss_period_correlation
        )
        if high_normal or high_loss:
            union(pair.left_candidate_id, pair.right_candidate_id)
    members_by_root: dict[str, list[str]] = {}
    for candidate_id in candidate_ids:
        members_by_root.setdefault(find(candidate_id), []).append(candidate_id)
    clusters: list[RedundancyCluster] = []
    for members in sorted(tuple(sorted(values)) for values in members_by_root.values()):
        member_tuple = tuple(members)
        cluster_id = hashlib.sha256(
            canonical_json_bytes(list(member_tuple))
        ).hexdigest()
        clusters.append(
            RedundancyCluster(
                cluster_id=cluster_id,
                candidate_ids=member_tuple,
            )
        )
    return tuple(clusters)


def _restrict_clusters(
    clusters: tuple[RedundancyCluster, ...],
    candidate_ids: tuple[str, ...],
) -> tuple[RedundancyCluster, ...]:
    allowed = set(candidate_ids)
    restricted: list[RedundancyCluster] = []
    for cluster in clusters:
        members = tuple(item for item in cluster.candidate_ids if item in allowed)
        if not members:
            continue
        restricted.append(
            RedundancyCluster(
                cluster_id=hashlib.sha256(
                    canonical_json_bytes(list(members))
                ).hexdigest(),
                candidate_ids=members,
            )
        )
    return tuple(restricted)


def _cluster_lookup(
    clusters: tuple[RedundancyCluster, ...],
    candidate_ids: tuple[str, ...],
) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for cluster in clusters:
        for candidate_id in cluster.candidate_ids:
            lookup[candidate_id] = cluster.cluster_id
    for candidate_id in candidate_ids:
        if candidate_id not in lookup:
            lookup[candidate_id] = hashlib.sha256(
                canonical_json_bytes([candidate_id])
            ).hexdigest()
    return lookup


def _cluster_weight(
    weights: Mapping[str, Decimal],
    cluster_for: Mapping[str, str],
    cluster_id: str,
) -> Decimal:
    return sum(
        (
            weight
            for candidate_id, weight in weights.items()
            if cluster_for[candidate_id] == cluster_id
        ),
        _ZERO,
    )


def _average_effective_strategy_count(
    snapshots: tuple[AllocationSnapshot, ...],
) -> float:
    if not snapshots:
        return 0.0
    values: list[float] = []
    for snapshot in snapshots:
        invested = sum((item.weight for item in snapshot.weights), _ZERO)
        squared = sum((item.weight * item.weight for item in snapshot.weights), _ZERO)
        if invested <= _ZERO or squared <= _ZERO:
            values.append(0.0)
        else:
            values.append(float(invested * invested / squared))
    return statistics.fmean(values)


def _largest_average_weight_candidate(
    snapshots: tuple[AllocationSnapshot, ...],
) -> str | None:
    totals: dict[str, Decimal] = {}
    for snapshot in snapshots:
        for item in snapshot.weights:
            totals[item.candidate_id] = (
                totals.get(item.candidate_id, _ZERO) + item.weight
            )
    if not totals:
        return None
    return max(sorted(totals), key=lambda candidate_id: totals[candidate_id])


def _cumulative_return(returns: Sequence[Decimal]) -> Decimal:
    equity = _ONE
    for value in returns:
        equity *= _ONE + value
    return equity - _ONE


def _maximum_drawdown(returns: Sequence[Decimal]) -> Decimal:
    equity = _ONE
    peak = _ONE
    maximum_drawdown = _ZERO
    for value in returns:
        equity *= _ONE + value
        peak = max(peak, equity)
        drawdown = equity / peak - _ONE
        maximum_drawdown = min(maximum_drawdown, drawdown)
    return maximum_drawdown


def _pearson(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right) or not left:
        raise PortfolioPromotionConfigurationError(
            "Correlation series must be nonempty and aligned."
        )
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    left_centered = tuple(value - left_mean for value in left)
    right_centered = tuple(value - right_mean for value in right)
    numerator = sum(
        left_value * right_value
        for left_value, right_value in zip(
            left_centered,
            right_centered,
            strict=True,
        )
    )
    left_scale = math.sqrt(sum(value * value for value in left_centered))
    right_scale = math.sqrt(sum(value * value for value in right_centered))
    if left_scale <= 0 or right_scale <= 0:
        return 0.0
    return max(-1.0, min(1.0, numerator / (left_scale * right_scale)))


def _spearman(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return _pearson(_average_ranks(left), _average_ranks(right))


def _average_ranks(values: tuple[float, ...]) -> tuple[float, ...]:
    ordered = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        average_rank = (index + 1 + end) / 2.0
        for position in range(index, end):
            ranks[ordered[position][0]] = average_rank
        index = end
    return tuple(ranks)


def _require_local_file(path: Path | str, field_name: str) -> Path:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise PortfolioPromotionConfigurationError(
            f"{field_name} does not exist: {source}"
        )
    if source.stat().st_size > _MAX_INPUT_BYTES:
        raise PortfolioPromotionConfigurationError(
            f"{field_name} exceeds the {_MAX_INPUT_BYTES}-byte limit."
        )
    return source


def _load_json(path: Path, field_name: str) -> dict[str, object]:
    source = _require_local_file(path, field_name)
    try:
        loaded = json.loads(source.read_text(encoding="utf-8"))
    except UnicodeDecodeError as error:
        raise PortfolioPromotionConfigurationError(
            f"{field_name} must be UTF-8 encoded."
        ) from error
    except json.JSONDecodeError as error:
        raise PortfolioPromotionConfigurationError(
            f"{field_name} is not valid JSON."
        ) from error
    if not isinstance(loaded, dict):
        raise PortfolioPromotionConfigurationError(
            f"{field_name} must contain a JSON object."
        )
    return cast(dict[str, object], loaded)


def _resolve_manifest_path(manifest_path: Path, raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = manifest_path.parent / candidate
    return _require_local_file(candidate, "Evidence report")



def _verified_manifest_file_sha256(
    candidate: Mapping[str, object],
    field_name: str,
    path: Path,
    candidate_id: str,
) -> str:
    expected = _required_sha256(candidate, field_name)
    actual = _sha256_file(path)
    if actual != expected:
        raise PortfolioPromotionIntegrityError(
            f"Evidence file SHA-256 mismatch for {candidate_id}: {field_name}."
        )
    return actual

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_dataset_binding(
    document: Mapping[str, object],
    dataset_digest: str,
    candidate_id: str,
    report_name: str,
) -> None:
    bindings = _values_for_keys_ending(document, "dataset_digest")
    if dataset_digest not in bindings:
        raise PortfolioPromotionIntegrityError(
            f"{report_name} for {candidate_id} is not bound to the manifest dataset."
        )


def _values_for_keys_ending(
    value: object,
    suffix: str,
) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for raw_key, child in value.items():
            if (
                isinstance(raw_key, str)
                and raw_key.lower().endswith(suffix)
                and isinstance(child, str)
            ):
                found.add(child)
            found.update(_values_for_keys_ending(child, suffix))
    elif isinstance(value, list):
        for child in value:
            found.update(_values_for_keys_ending(child, suffix))
    return found


def _required_string(document: Mapping[str, object], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PortfolioPromotionConfigurationError(
            f"Manifest field {key!r} must be a nonblank string."
        )
    return value.strip()


def _required_bool(document: Mapping[str, object], key: str) -> bool:
    value = document.get(key)
    if not isinstance(value, bool):
        raise PortfolioPromotionIntegrityError(
            f"Evidence field {key!r} must be a boolean."
        )
    return value


def _required_sha256(document: Mapping[str, object], key: str) -> str:
    value = document.get(key)
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value.lower() != value
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise PortfolioPromotionIntegrityError(
            f"Evidence field {key!r} must be a lowercase SHA-256 digest."
        )
    return value



def _nested_optional_decimal(
    document: Mapping[str, object],
    path: tuple[str, ...],
) -> Decimal | None:
    current: object = document
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if current is None:
        return None
    if not isinstance(current, (str, int, float)) or isinstance(current, bool):
        raise PortfolioPromotionIntegrityError(
            f"Backtest field {'.'.join(path)!r} must be numeric."
        )
    try:
        value = Decimal(str(current))
    except InvalidOperation as error:
        raise PortfolioPromotionIntegrityError(
            f"Backtest field {'.'.join(path)!r} is not a valid decimal."
        ) from error
    if not value.is_finite():
        raise PortfolioPromotionIntegrityError(
            f"Backtest field {'.'.join(path)!r} must be finite."
        )
    return value


def _backtest_cost_to_gross_profit_ratio(
    document: Mapping[str, object],
) -> Decimal | None:
    initial = _nested_optional_decimal(document, ("metrics", "initial_equity"))
    final = _nested_optional_decimal(document, ("metrics", "final_equity"))
    commission = _nested_optional_decimal(
        document,
        ("metrics", "commission_cost"),
    )
    slippage = _nested_optional_decimal(
        document,
        ("metrics", "slippage_cost"),
    )
    values = (initial, final, commission, slippage)
    if any(value is None for value in values):
        return None
    assert initial is not None
    assert final is not None
    assert commission is not None
    assert slippage is not None
    if initial <= _ZERO or commission < _ZERO or slippage < _ZERO:
        raise PortfolioPromotionIntegrityError(
            "Backtest economic metrics contain invalid values."
        )
    total_cost = commission + slippage
    gross_profit = final - initial + total_cost
    if gross_profit <= _ZERO:
        return None
    return total_cost / gross_profit


def _reconcile_economic_evidence(
    *,
    declared: Decimal | None,
    reported: Decimal | None,
    candidate_id: str,
    field_name: str,
) -> Decimal | None:
    if declared is not None and reported is not None and declared != reported:
        raise PortfolioPromotionIntegrityError(
            f"Declared {field_name} conflicts with backtest evidence for "
            f"{candidate_id}."
        )
    return reported if reported is not None else declared

def _optional_decimal(
    document: Mapping[str, object],
    key: str,
) -> Decimal | None:
    value = document.get(key)
    if value is None:
        return None
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise PortfolioPromotionConfigurationError(
            f"Manifest field {key!r} must be numeric or null."
        )
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise PortfolioPromotionConfigurationError(
            f"Manifest field {key!r} is not a valid decimal."
        ) from error
    if not result.is_finite() or result < _ZERO:
        raise PortfolioPromotionConfigurationError(
            f"Manifest field {key!r} must be finite and nonnegative."
        )
    return result


def _parse_timestamp(value: str, line_number: int) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise PortfolioPromotionConfigurationError(
            f"Returns CSV line {line_number} has an invalid timestamp."
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PortfolioPromotionConfigurationError(
            f"Returns CSV line {line_number} timestamp must be timezone-aware."
        )
    return parsed.astimezone(UTC)


def _parse_return(
    value: str,
    line_number: int,
    candidate_id: str,
) -> Decimal:
    try:
        parsed = Decimal(value.strip())
    except InvalidOperation as error:
        raise PortfolioPromotionConfigurationError(
            f"Returns CSV line {line_number} has an invalid return for {candidate_id}."
        ) from error
    if not parsed.is_finite() or parsed <= -_ONE:
        raise PortfolioPromotionConfigurationError(
            f"Returns CSV line {line_number} return for {candidate_id} "
            "must be finite and greater than -1."
        )
    return parsed


__all__ = [
    "DeterministicPortfolioPromotionEngine",
    "load_candidate_evidence_manifest",
    "parse_candidate_returns_csv",
]
