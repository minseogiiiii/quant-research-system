from __future__ import annotations

import json
import math
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from world_quant_system.research.historical_shadow_models import (
    HistoricalShadowConfigurationError,
    HistoricalShadowEligibilityError,
    HistoricalShadowIntegrityError,
    HistoricalShadowPolicy,
    HistoricalShadowReport,
    ShadowEvidenceEvent,
    ShadowEvidenceManifest,
    ShadowEvidenceState,
    ShadowJournalEntry,
    ShadowJournalEventType,
    ShadowLedgerSnapshot,
    ShadowMetrics,
    ShadowRunDecision,
    ShadowScenarioName,
    ShadowScenarioResult,
    ShadowWeight,
    parse_utc_datetime,
)
from world_quant_system.research.portfolio_promotion_models import (
    AllocationMethod,
    CandidateReturnMatrix,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_BPS = Decimal("10000")
_WEIGHT_EPSILON = Decimal("0.000000000001")


@dataclass(frozen=True, slots=True)
class _PendingAllocation:
    execute_index: int
    weights: Mapping[str, Decimal]
    cash_weight: Decimal
    turnover: Decimal
    reason: str


@dataclass(frozen=True, slots=True)
class _SimulationResult:
    ledger: tuple[ShadowLedgerSnapshot, ...]
    journal: tuple[ShadowJournalEntry, ...]
    metrics: ShadowMetrics
    average_weights: Mapping[str, Decimal]


class PointInTimeEvidenceResolver:
    """Resolve only evidence that was available at a historical timestamp."""

    def __init__(self, manifest: ShadowEvidenceManifest) -> None:
        if not isinstance(manifest, ShadowEvidenceManifest):
            raise HistoricalShadowConfigurationError(
                "Point-in-time resolver requires a ShadowEvidenceManifest."
            )
        self._events_by_candidate: dict[str, tuple[ShadowEvidenceEvent, ...]] = {}
        for candidate_id in manifest.candidate_ids:
            self._events_by_candidate[candidate_id] = tuple(
                item
                for item in manifest.events
                if item.candidate_id == candidate_id
            )

    def visible_event(
        self,
        candidate_id: str,
        timestamp: datetime,
    ) -> ShadowEvidenceEvent | None:
        events = self._events_by_candidate.get(candidate_id, ())
        visible: ShadowEvidenceEvent | None = None
        normalized = timestamp.astimezone(UTC)
        for evidence_event in events:
            if evidence_event.available_at.astimezone(UTC) > normalized:
                break
            visible = evidence_event
        return visible

    def eligible_candidates(
        self,
        timestamp: datetime,
        *,
        maximum_age_days: int,
        excluded_candidate_ids: frozenset[str] = frozenset(),
    ) -> tuple[str, ...]:
        normalized = timestamp.astimezone(UTC)
        eligible: list[str] = []
        for candidate_id in sorted(self._events_by_candidate):
            if candidate_id in excluded_candidate_ids:
                continue
            visible = self.visible_event(candidate_id, normalized)
            if visible is None or visible.state is not ShadowEvidenceState.PROMOTED:
                continue
            age_days = (normalized - visible.available_at.astimezone(UTC)).days
            if age_days > maximum_age_days:
                continue
            eligible.append(candidate_id)
        return tuple(eligible)


class DeterministicHistoricalShadowRunner:
    """Run point-in-time, next-period portfolio shadow simulation."""

    def __init__(
        self,
        *,
        evidence_manifest: ShadowEvidenceManifest,
        return_matrix: CandidateReturnMatrix,
        policy: HistoricalShadowPolicy,
    ) -> None:
        if not isinstance(evidence_manifest, ShadowEvidenceManifest):
            raise HistoricalShadowConfigurationError(
                "Historical shadow runner requires an evidence manifest."
            )
        if not isinstance(return_matrix, CandidateReturnMatrix):
            raise HistoricalShadowConfigurationError(
                "Historical shadow runner requires a candidate return matrix."
            )
        if not isinstance(policy, HistoricalShadowPolicy):
            raise HistoricalShadowConfigurationError(
                "Historical shadow runner requires a valid policy."
            )
        if evidence_manifest.return_matrix_digest != return_matrix.matrix_digest:
            raise HistoricalShadowIntegrityError(
                "Evidence manifest return-matrix digest does not match input CSV."
            )
        missing = set(evidence_manifest.candidate_ids) - set(
            return_matrix.candidate_ids
        )
        if missing:
            raise HistoricalShadowIntegrityError(
                "Evidence candidates are missing return series: "
                + ", ".join(sorted(missing))
            )
        if return_matrix.observation_count < policy.minimum_observations:
            raise HistoricalShadowEligibilityError(
                "Return matrix does not satisfy the minimum observation policy."
            )
        self._manifest = evidence_manifest
        self._matrix = return_matrix
        self._policy = policy
        self._resolver = PointInTimeEvidenceResolver(evidence_manifest)

    def run(self, *, created_at: datetime | None = None) -> HistoricalShadowReport:
        base = self._simulate(
            allocation_method=self._policy.allocation_method,
            transaction_cost_bps=self._policy.transaction_cost_bps,
            execution_delay_periods=self._policy.execution_delay_periods,
            excluded_candidate_ids=frozenset(),
            include_journal=True,
        )
        equal_weight = self._simulate(
            allocation_method=AllocationMethod.EQUAL_WEIGHT,
            transaction_cost_bps=self._policy.transaction_cost_bps,
            execution_delay_periods=self._policy.execution_delay_periods,
            excluded_candidate_ids=frozenset(),
            include_journal=False,
        )
        double_cost = self._simulate(
            allocation_method=self._policy.allocation_method,
            transaction_cost_bps=self._policy.transaction_cost_bps * Decimal("2"),
            execution_delay_periods=self._policy.execution_delay_periods,
            excluded_candidate_ids=frozenset(),
            include_journal=False,
        )
        extra_delay = self._simulate(
            allocation_method=self._policy.allocation_method,
            transaction_cost_bps=self._policy.transaction_cost_bps,
            execution_delay_periods=self._policy.execution_delay_periods + 1,
            excluded_candidate_ids=frozenset(),
            include_journal=False,
        )
        largest_candidate = _largest_average_weight(base.average_weights)
        removed = self._simulate(
            allocation_method=self._policy.allocation_method,
            transaction_cost_bps=self._policy.transaction_cost_bps,
            execution_delay_periods=self._policy.execution_delay_periods,
            excluded_candidate_ids=(
                frozenset({largest_candidate})
                if largest_candidate is not None
                else frozenset()
            ),
            include_journal=False,
        )
        scenarios = (
            _scenario_result(ShadowScenarioName.BASE, base, self._policy),
            _scenario_result(
                ShadowScenarioName.EQUAL_WEIGHT_BASELINE,
                equal_weight,
                self._policy,
            ),
            _scenario_result(
                ShadowScenarioName.DOUBLE_COST,
                double_cost,
                self._policy,
            ),
            _scenario_result(
                ShadowScenarioName.EXTRA_EXECUTION_DELAY,
                extra_delay,
                self._policy,
            ),
            _scenario_result(
                ShadowScenarioName.LARGEST_CANDIDATE_REMOVED,
                removed,
                self._policy,
                removed_candidate_id=largest_candidate,
            ),
        )
        decision_reasons = tuple(
            reason
            for scenario in scenarios
            if scenario.scenario is not ShadowScenarioName.EQUAL_WEIGHT_BASELINE
            for reason in scenario.reasons
        )
        if not any(snapshot.weights for snapshot in base.ledger):
            decision = ShadowRunDecision.INSUFFICIENT_EVIDENCE
            if not decision_reasons:
                decision_reasons = (
                    "no point-in-time candidate allocation was executed",
                )
        elif decision_reasons:
            decision = ShadowRunDecision.REJECTED
        else:
            decision = ShadowRunDecision.READY_FOR_FORWARD_SHADOW_RESEARCH
        report_created_at = created_at or datetime.now(UTC)
        return HistoricalShadowReport(
            dataset_digest=self._manifest.dataset_digest,
            matrix_digest=self._matrix.matrix_digest,
            manifest_digest=self._manifest.manifest_digest,
            policy=self._policy,
            journal=base.journal,
            ledger=base.ledger,
            scenarios=scenarios,
            decision=decision,
            decision_reasons=decision_reasons,
            created_at=report_created_at,
        )

    def _simulate(
        self,
        *,
        allocation_method: AllocationMethod,
        transaction_cost_bps: Decimal,
        execution_delay_periods: int,
        excluded_candidate_ids: frozenset[str],
        include_journal: bool,
    ) -> _SimulationResult:
        equity = self._policy.initial_equity
        peak_equity = equity
        active_weights: dict[str, Decimal] = {}
        cash_weight = _ONE
        pending: _PendingAllocation | None = None
        risk_halted = False
        journal: list[ShadowJournalEntry] = []
        ledger: list[ShadowLedgerSnapshot] = []
        visible_event_ids: dict[str, str] = {}
        average_weight_sums = {
            candidate_id: _ZERO for candidate_id in self._matrix.candidate_ids
        }
        for index, timestamp in enumerate(self._matrix.timestamps):
            normalized_timestamp = timestamp.astimezone(UTC)
            execution_cost = _ZERO
            executed_turnover = _ZERO
            if pending is not None and pending.execute_index == index:
                execution_cost = (
                    equity * pending.turnover * transaction_cost_bps / _BPS
                )
                if execution_cost >= equity:
                    raise HistoricalShadowIntegrityError(
                        "Transaction cost would exhaust shadow equity."
                    )
                equity -= execution_cost
                active_weights = dict(pending.weights)
                cash_weight = pending.cash_weight
                executed_turnover = pending.turnover
                if include_journal:
                    _append_journal(
                        journal,
                        timestamp=normalized_timestamp,
                        event_type=ShadowJournalEventType.REBALANCE_EXECUTED,
                        candidate_id=None,
                        reason=pending.reason,
                    )
                pending = None
            equity_before = equity
            gross_period_return = sum(
                (
                    weight
                    * self._matrix.series_for(candidate_id)[index]
                    for candidate_id, weight in active_weights.items()
                ),
                _ZERO,
            )
            equity *= _ONE + gross_period_return
            if equity <= _ZERO:
                raise HistoricalShadowIntegrityError(
                    "Historical shadow equity became nonpositive."
                )
            peak_equity = max(peak_equity, equity)
            drawdown = equity / peak_equity - _ONE
            halt_triggered = (
                not risk_halted
                and drawdown <= self._policy.maximum_drawdown_before_halt
            )
            if halt_triggered:
                risk_halted = True
            net_period_return = equity / (
                equity_before + execution_cost
            ) - _ONE
            ledger.append(
                ShadowLedgerSnapshot(
                    timestamp=normalized_timestamp,
                    equity_before=equity_before + execution_cost,
                    execution_cost=execution_cost,
                    gross_period_return=gross_period_return,
                    net_period_return=net_period_return,
                    equity_after=equity,
                    drawdown=drawdown,
                    weights=tuple(
                        ShadowWeight(candidate_id=item, weight=active_weights[item])
                        for item in sorted(active_weights)
                        if active_weights[item] > _WEIGHT_EPSILON
                    ),
                    cash_weight=cash_weight,
                    one_way_turnover=executed_turnover,
                    risk_halted=risk_halted,
                )
            )
            for candidate_id, active_weight in active_weights.items():
                average_weight_sums[candidate_id] += active_weight
            if include_journal:
                self._record_new_visible_evidence(
                    normalized_timestamp,
                    visible_event_ids,
                    journal,
                    excluded_candidate_ids,
                )
            if halt_triggered:
                pending = _PendingAllocation(
                    execute_index=min(
                        index + execution_delay_periods,
                        len(self._matrix.timestamps) - 1,
                    ),
                    weights={},
                    cash_weight=_ONE,
                    turnover=_one_way_turnover(
                        active_weights,
                        cash_weight,
                        {},
                        _ONE,
                    ),
                    reason="risk halt moved the portfolio to cash",
                )
                if include_journal:
                    _append_journal(
                        journal,
                        timestamp=normalized_timestamp,
                        event_type=ShadowJournalEventType.RISK_HALT_TRIGGERED,
                        candidate_id=None,
                        reason=(
                            "portfolio drawdown reached the fail-closed halt threshold"
                        ),
                    )
                continue
            if risk_halted or index % self._policy.rebalance_frequency != 0:
                continue
            if index + execution_delay_periods >= len(self._matrix.timestamps):
                continue
            if pending is not None:
                if include_journal:
                    _append_journal(
                        journal,
                        timestamp=normalized_timestamp,
                        event_type=ShadowJournalEventType.REBALANCE_REJECTED,
                        candidate_id=None,
                        reason="a prior allocation is still pending execution",
                    )
                continue
            eligible = self._resolver.eligible_candidates(
                normalized_timestamp,
                maximum_age_days=self._policy.maximum_evidence_age_days,
                excluded_candidate_ids=excluded_candidate_ids,
            )
            if len(eligible) < self._policy.minimum_active_candidates:
                if include_journal:
                    _append_journal(
                        journal,
                        timestamp=normalized_timestamp,
                        event_type=(
                            ShadowJournalEventType.NO_ELIGIBLE_CANDIDATES
                        ),
                        candidate_id=None,
                        reason=(
                            "point-in-time evidence did not satisfy the minimum "
                            "active-candidate policy"
                        ),
                    )
                continue
            target_weights, target_cash = self._allocate(
                eligible,
                as_of_index=index,
                allocation_method=allocation_method,
            )
            turnover = _one_way_turnover(
                active_weights,
                cash_weight,
                target_weights,
                target_cash,
            )
            if turnover > self._policy.maximum_one_way_turnover:
                if include_journal:
                    _append_journal(
                        journal,
                        timestamp=normalized_timestamp,
                        event_type=ShadowJournalEventType.REBALANCE_REJECTED,
                        candidate_id=None,
                        reason=(
                            "proposed allocation exceeded the one-way turnover limit"
                        ),
                    )
                continue
            pending = _PendingAllocation(
                execute_index=index + execution_delay_periods,
                weights=target_weights,
                cash_weight=target_cash,
                turnover=turnover,
                reason=(
                    f"{allocation_method.value} allocation executed after "
                    f"{execution_delay_periods} period(s)"
                ),
            )
            if include_journal:
                _append_journal(
                    journal,
                    timestamp=normalized_timestamp,
                    event_type=ShadowJournalEventType.REBALANCE_SCHEDULED,
                    candidate_id=None,
                    reason=(
                        "allocation used only evidence and returns observable at "
                        "the decision timestamp"
                    ),
                )
        metrics = _metrics(tuple(ledger), self._policy)
        divisor = Decimal(len(ledger))
        average_weights = {
            candidate_id: total / divisor
            for candidate_id, total in average_weight_sums.items()
        }
        return _SimulationResult(
            ledger=tuple(ledger),
            journal=tuple(journal),
            metrics=metrics,
            average_weights=average_weights,
        )

    def _allocate(
        self,
        eligible: tuple[str, ...],
        *,
        as_of_index: int,
        allocation_method: AllocationMethod,
    ) -> tuple[dict[str, Decimal], Decimal]:
        investable = _ONE - self._policy.minimum_cash_weight
        scores = (
            {candidate_id: _ONE for candidate_id in eligible}
            if allocation_method is AllocationMethod.EQUAL_WEIGHT
            else self._inverse_volatility_scores(eligible, as_of_index)
        )
        weights = _capped_weights(
            scores,
            investable=investable,
            maximum_candidate_weight=self._policy.maximum_candidate_weight,
        )
        allocated = sum(weights.values(), _ZERO)
        return weights, _ONE - allocated

    def _inverse_volatility_scores(
        self,
        eligible: tuple[str, ...],
        as_of_index: int,
    ) -> dict[str, Decimal]:
        start = max(0, as_of_index + 1 - self._policy.volatility_lookback)
        scores: dict[str, Decimal] = {}
        for candidate_id in eligible:
            historical_values = tuple(
                float(value)
                for value in self._matrix.series_for(candidate_id)[
                    start : as_of_index + 1
                ]
            )
            standard_deviation = (
                statistics.stdev(historical_values)
                if len(historical_values) >= 2
                else 0.0
            )
            scores[candidate_id] = (
                _ONE
                if standard_deviation <= 0
                else Decimal(str(1.0 / standard_deviation))
            )
        return scores

    def _record_new_visible_evidence(
        self,
        timestamp: datetime,
        visible_event_ids: dict[str, str],
        journal: list[ShadowJournalEntry],
        excluded_candidate_ids: frozenset[str],
    ) -> None:
        for candidate_id in self._manifest.candidate_ids:
            if candidate_id in excluded_candidate_ids:
                continue
            visible = self._resolver.visible_event(candidate_id, timestamp)
            if (
                visible is None
                or visible_event_ids.get(candidate_id) == visible.event_id
            ):
                continue
            visible_event_ids[candidate_id] = visible.event_id
            _append_journal(
                journal,
                timestamp=timestamp,
                event_type=ShadowJournalEventType.EVIDENCE_VISIBLE,
                candidate_id=candidate_id,
                reason=(
                    f"{visible.state.value} evidence became visible: {visible.reason}"
                ),
                related_event_id=visible.event_id,
            )
            eligibility_event = (
                ShadowJournalEventType.CANDIDATE_ELIGIBLE
                if visible.state is ShadowEvidenceState.PROMOTED
                else ShadowJournalEventType.CANDIDATE_INELIGIBLE
            )
            _append_journal(
                journal,
                timestamp=timestamp,
                event_type=eligibility_event,
                candidate_id=candidate_id,
                reason=f"candidate state changed to {visible.state.value}",
                related_event_id=visible.event_id,
            )


def load_shadow_evidence_manifest(path: Path) -> ShadowEvidenceManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise HistoricalShadowConfigurationError(
            f"Unable to read evidence manifest: {path}"
        ) from error
    except json.JSONDecodeError as error:
        raise HistoricalShadowConfigurationError(
            "Evidence manifest is not valid JSON."
        ) from error
    if not isinstance(raw, dict):
        raise HistoricalShadowConfigurationError(
            "Evidence manifest root must be a JSON object."
        )
    schema_version = raw.get("schema_version")
    if schema_version != 1:
        raise HistoricalShadowConfigurationError(
            "Evidence manifest schema_version must equal 1."
        )
    dataset_digest = _required_text(raw, "dataset_digest")
    return_matrix_digest = _required_text(raw, "return_matrix_digest")
    raw_events = raw.get("events")
    if not isinstance(raw_events, list):
        raise HistoricalShadowConfigurationError(
            "Evidence manifest events must be a JSON array."
        )
    events = tuple(
        sorted(
            (_parse_evidence_event(item) for item in raw_events),
            key=lambda event: (
                event.available_at.astimezone(UTC),
                event.candidate_id,
                event.event_id,
            ),
        )
    )
    return ShadowEvidenceManifest(
        dataset_digest=dataset_digest,
        return_matrix_digest=return_matrix_digest,
        events=events,
    )


def _parse_evidence_event(raw: object) -> ShadowEvidenceEvent:
    if not isinstance(raw, dict):
        raise HistoricalShadowConfigurationError(
            "Each evidence event must be a JSON object."
        )
    try:
        state = ShadowEvidenceState(_required_text(raw, "state"))
    except ValueError as error:
        raise HistoricalShadowConfigurationError(
            "Evidence state is unsupported."
        ) from error
    return ShadowEvidenceEvent(
        candidate_id=_required_text(raw, "candidate_id"),
        strategy_name=_required_text(raw, "strategy_name"),
        strategy_version=_required_text(raw, "strategy_version"),
        state=state,
        effective_at=parse_utc_datetime(raw.get("effective_at"), "effective_at"),
        available_at=parse_utc_datetime(raw.get("available_at"), "available_at"),
        evidence_digest=_required_text(raw, "evidence_digest"),
        reason=_required_text(raw, "reason"),
    )


def _required_text(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HistoricalShadowConfigurationError(
            f"Evidence manifest field {key!r} must be a nonblank string."
        )
    return value.strip()


def _capped_weights(
    scores: Mapping[str, Decimal],
    *,
    investable: Decimal,
    maximum_candidate_weight: Decimal,
) -> dict[str, Decimal]:
    remaining = investable
    weights = {candidate_id: _ZERO for candidate_id in sorted(scores)}
    active = set(weights)
    while active and remaining > _WEIGHT_EPSILON:
        score_total = sum((scores[item] for item in active), _ZERO)
        normalized_scores = (
            {item: _ONE for item in active}
            if score_total <= _ZERO
            else {item: scores[item] for item in active}
        )
        score_total = (
            Decimal(len(active)) if score_total <= _ZERO else score_total
        )
        allocated = _ZERO
        saturated: set[str] = set()
        for candidate_id in sorted(active):
            room = maximum_candidate_weight - weights[candidate_id]
            proposed = remaining * normalized_scores[candidate_id] / score_total
            increment = min(room, proposed)
            if increment > _ZERO:
                weights[candidate_id] += increment
                allocated += increment
            if room - increment <= _WEIGHT_EPSILON:
                saturated.add(candidate_id)
        if allocated <= _WEIGHT_EPSILON:
            break
        remaining -= allocated
        active -= saturated
    return {
        candidate_id: weight
        for candidate_id, weight in weights.items()
        if weight > _WEIGHT_EPSILON
    }


def _one_way_turnover(
    old_weights: Mapping[str, Decimal],
    old_cash: Decimal,
    new_weights: Mapping[str, Decimal],
    new_cash: Decimal,
) -> Decimal:
    candidates = set(old_weights) | set(new_weights)
    total_change = sum(
        (
            abs(
                new_weights.get(candidate_id, _ZERO)
                - old_weights.get(candidate_id, _ZERO)
            )
            for candidate_id in candidates
        ),
        abs(new_cash - old_cash),
    )
    return total_change / Decimal("2")


def _metrics(
    ledger: tuple[ShadowLedgerSnapshot, ...],
    policy: HistoricalShadowPolicy,
) -> ShadowMetrics:
    if not ledger:
        raise HistoricalShadowEligibilityError(
            "Historical shadow metrics require ledger observations."
        )
    periodic_returns = tuple(item.net_period_return for item in ledger)
    float_returns = tuple(float(value) for value in periodic_returns)
    initial_equity = policy.initial_equity
    final_equity = ledger[-1].equity_after
    cumulative_return = final_equity / initial_equity - _ONE
    annualized_return = (
        float(final_equity / initial_equity)
        ** (policy.annualization_periods / len(ledger))
        - 1.0
        if final_equity > _ZERO
        else None
    )
    standard_deviation = (
        statistics.stdev(float_returns) if len(float_returns) >= 2 else 0.0
    )
    annualized_volatility = standard_deviation * math.sqrt(
        policy.annualization_periods
    )
    sharpe_ratio = (
        statistics.fmean(float_returns)
        / standard_deviation
        * math.sqrt(policy.annualization_periods)
        if standard_deviation > 0
        else None
    )
    downside = tuple(min(0.0, item) for item in float_returns)
    downside_deviation = math.sqrt(
        statistics.fmean(item * item for item in downside)
    )
    sortino_ratio = (
        statistics.fmean(float_returns)
        / downside_deviation
        * math.sqrt(policy.annualization_periods)
        if downside_deviation > 0
        else None
    )
    tail_count = max(
        1,
        math.ceil(
            len(periodic_returns) * float(policy.expected_shortfall_fraction)
        ),
    )
    expected_shortfall = sum(
        sorted(periodic_returns)[:tail_count],
        _ZERO,
    ) / Decimal(tail_count)
    average_cash_weight = sum(
        (item.cash_weight for item in ledger),
        _ZERO,
    ) / Decimal(len(ledger))
    effective_counts = tuple(
        _effective_strategy_count(item.weights) for item in ledger
    )
    return ShadowMetrics(
        observation_count=len(ledger),
        initial_equity=initial_equity,
        final_equity=final_equity,
        cumulative_return=cumulative_return,
        annualized_return=annualized_return,
        annualized_volatility=annualized_volatility,
        sharpe_ratio=sharpe_ratio,
        sortino_ratio=sortino_ratio,
        maximum_drawdown=min(item.drawdown for item in ledger),
        expected_shortfall=expected_shortfall,
        total_turnover=sum(
            (item.one_way_turnover for item in ledger),
            _ZERO,
        ),
        total_cost=sum((item.execution_cost for item in ledger), _ZERO),
        average_cash_weight=average_cash_weight,
        average_effective_strategy_count=statistics.fmean(effective_counts),
        risk_halt_count=int(any(item.risk_halted for item in ledger)),
    )


def _effective_strategy_count(weights: Iterable[ShadowWeight]) -> float:
    weight_values = tuple(float(item.weight) for item in weights)
    invested = sum(weight_values)
    squares = sum(item * item for item in weight_values)
    return 0.0 if squares == 0 else invested * invested / squares


def _scenario_result(
    scenario: ShadowScenarioName,
    result: _SimulationResult,
    policy: HistoricalShadowPolicy,
    *,
    removed_candidate_id: str | None = None,
) -> ShadowScenarioResult:
    reasons: list[str] = []
    if (
        scenario is ShadowScenarioName.BASE
        and result.metrics.cumulative_return
        < policy.minimum_base_cumulative_return
    ):
        reasons.append("base cumulative return below policy minimum")
    elif scenario is not ShadowScenarioName.EQUAL_WEIGHT_BASELINE:
        if (
            result.metrics.cumulative_return
            < policy.minimum_stress_cumulative_return
        ):
            reasons.append("stress cumulative return below policy minimum")
        if result.metrics.maximum_drawdown < policy.maximum_stress_drawdown:
            reasons.append("stress drawdown exceeds policy maximum")
    return ShadowScenarioResult(
        scenario=scenario,
        metrics=result.metrics,
        removed_candidate_id=removed_candidate_id,
        passed=not reasons,
        reasons=tuple(reasons),
    )


def _largest_average_weight(
    average_weights: Mapping[str, Decimal],
) -> str | None:
    positive = tuple(
        (candidate_id, weight)
        for candidate_id, weight in average_weights.items()
        if weight > _ZERO
    )
    if not positive:
        return None
    return min(
        positive,
        key=lambda item: (-item[1], item[0]),
    )[0]


def _append_journal(
    journal: list[ShadowJournalEntry],
    *,
    timestamp: datetime,
    event_type: ShadowJournalEventType,
    candidate_id: str | None,
    reason: str,
    related_event_id: str | None = None,
) -> None:
    journal.append(
        ShadowJournalEntry(
            sequence=len(journal),
            timestamp=timestamp,
            event_type=event_type,
            candidate_id=candidate_id,
            reason=reason,
            related_event_id=related_event_id,
        )
    )


def decimal_argument(value: str, field_name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise HistoricalShadowConfigurationError(
            f"{field_name} must be a valid decimal."
        ) from error
    if not parsed.is_finite():
        raise HistoricalShadowConfigurationError(
            f"{field_name} must be finite."
        )
    return parsed
