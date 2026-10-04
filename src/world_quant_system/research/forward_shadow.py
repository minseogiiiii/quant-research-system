from __future__ import annotations

import json
import statistics
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from world_quant_system.research.forward_shadow_models import (
    ForwardShadowBatchReport,
    ForwardShadowConfigurationError,
    ForwardShadowIntegrityError,
    ForwardShadowJournalEntry,
    ForwardShadowJournalEventType,
    ForwardShadowLedgerSnapshot,
    ForwardShadowObservation,
    ForwardShadowPolicy,
    ForwardShadowState,
    PendingForwardAllocation,
    parse_forward_shadow_observation,
)
from world_quant_system.research.historical_shadow import (
    PointInTimeEvidenceResolver,
)
from world_quant_system.research.historical_shadow_models import (
    ShadowEvidenceManifest,
    ShadowEvidenceState,
    ShadowWeight,
)
from world_quant_system.research.portfolio_promotion_models import (
    AllocationMethod,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_BPS = Decimal("10000")
_GENESIS_HASH = "0" * 64


class ForwardShadowClock(Protocol):
    """Return the current timezone-aware UTC timestamp."""

    def now_utc(self) -> datetime:
        """Return current time in UTC."""
        ...


class SystemForwardShadowClock:
    def now_utc(self) -> datetime:
        return datetime.now(UTC)


class FixedForwardShadowClock:
    def __init__(self, now: datetime) -> None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ForwardShadowConfigurationError(
                "Fixed clock time must be timezone-aware."
            )
        self._now = now.astimezone(UTC)

    def now_utc(self) -> datetime:
        return self._now


class DeterministicForwardShadowRunner:
    """Advance append-only forward-shadow state without broker interaction."""

    def __init__(
        self,
        *,
        evidence_manifest: ShadowEvidenceManifest,
        policy: ForwardShadowPolicy,
        clock: ForwardShadowClock | None = None,
    ) -> None:
        if not isinstance(evidence_manifest, ShadowEvidenceManifest):
            raise ForwardShadowConfigurationError(
                "Forward shadow requires a ShadowEvidenceManifest."
            )
        if not isinstance(policy, ForwardShadowPolicy):
            raise ForwardShadowConfigurationError(
                "Forward shadow requires a ForwardShadowPolicy."
            )
        self._manifest = evidence_manifest
        self._policy = policy
        self._clock = clock or SystemForwardShadowClock()
        self._resolver = PointInTimeEvidenceResolver(evidence_manifest)

    def initial_state(self) -> ForwardShadowState:
        return ForwardShadowState(
            dataset_digest=self._manifest.dataset_digest,
            manifest_digest=self._manifest.manifest_digest,
            manifest_event_ids=tuple(
                sorted(item.event_id for item in self._manifest.events)
            ),
            policy=self._policy,
            observations=(),
            journal=(),
            ledger=(),
            active_weights=(),
            cash_weight=_ONE,
            pending_allocation=None,
            equity=self._policy.initial_equity,
            peak_equity=self._policy.initial_equity,
            risk_halted=False,
            feed_stale=False,
            visible_event_ids=(),
            updated_at=None,
        )

    def process(
        self,
        observations: Sequence[ForwardShadowObservation],
        *,
        state: ForwardShadowState | None = None,
        generated_at: datetime | None = None,
    ) -> ForwardShadowBatchReport:
        current = state or self.initial_state()
        self._validate_state_identity(current)
        processed = 0
        duplicates = 0
        for observation in observations:
            if not isinstance(observation, ForwardShadowObservation):
                raise ForwardShadowConfigurationError(
                    "Forward-shadow batches require observation objects."
                )
            if observation.observation_id in current.observation_ids:
                duplicates += 1
                continue
            current = self._advance(current, observation)
            processed += 1
        report_time = generated_at or self._clock.now_utc()
        return ForwardShadowBatchReport(
            state=current,
            processed_observations=processed,
            duplicate_observations=duplicates,
            generated_at=report_time,
        )

    def _validate_state_identity(self, state: ForwardShadowState) -> None:
        if not isinstance(state, ForwardShadowState):
            raise ForwardShadowConfigurationError(
                "Forward-shadow runner state is invalid."
            )
        if state.dataset_digest != self._manifest.dataset_digest:
            raise ForwardShadowIntegrityError(
                "State dataset digest does not match the evidence manifest."
            )
        current_event_ids = frozenset(
            item.event_id for item in self._manifest.events
        )
        if not set(state.manifest_event_ids).issubset(current_event_ids):
            raise ForwardShadowIntegrityError(
                "Current evidence manifest removed or changed prior events."
            )
        if (
            state.manifest_digest == self._manifest.manifest_digest
            and set(state.manifest_event_ids) != current_event_ids
        ):
            raise ForwardShadowIntegrityError(
                "State manifest identity is inconsistent with event IDs."
            )
        if state.updated_at is not None:
            prior_event_ids = set(state.manifest_event_ids)
            backfilled = tuple(
                item
                for item in self._manifest.events
                if item.event_id not in prior_event_ids
                and item.available_at <= state.updated_at
            )
            if backfilled:
                raise ForwardShadowIntegrityError(
                    "New evidence events cannot be backfilled before the checkpoint."
                )
        if state.policy != self._policy:
            raise ForwardShadowIntegrityError(
                "State policy does not match the configured policy."
            )

    def _advance(
        self,
        state: ForwardShadowState,
        observation: ForwardShadowObservation,
    ) -> ForwardShadowState:
        self._validate_sequence_and_time(state, observation)
        expected_candidates = set(self._manifest.candidate_ids)
        actual_candidates = set(observation.returns_by_candidate)
        if actual_candidates != expected_candidates:
            missing = sorted(expected_candidates - actual_candidates)
            extra = sorted(actual_candidates - expected_candidates)
            details = []
            if missing:
                details.append("missing=" + ",".join(missing))
            if extra:
                details.append("extra=" + ",".join(extra))
            raise ForwardShadowIntegrityError(
                "Observation candidate set differs from evidence manifest: "
                + "; ".join(details)
            )

        observations = (*state.observations, observation)
        journal = list(state.journal)
        visible_event_ids = state.visible_event_map
        active_weights = {
            item.candidate_id: item.weight for item in state.active_weights
        }
        cash_weight = state.cash_weight
        pending = state.pending_allocation
        equity_before = state.equity
        peak_equity = state.peak_equity
        risk_halted = state.risk_halted
        feed_stale = state.feed_stale
        execution_cost = _ZERO
        turnover = _ZERO

        _append_journal(
            journal,
            timestamp=observation.received_at,
            event_type=ForwardShadowJournalEventType.OBSERVATION_ACCEPTED,
            reason="append-only observation accepted after clock and sequence checks",
            observation_id=observation.observation_id,
        )
        self._record_visible_evidence(
            observation,
            visible_event_ids,
            journal,
        )

        if pending is not None and pending.execute_sequence == observation.sequence:
            active_weights = {
                item.candidate_id: item.weight for item in pending.weights
            }
            cash_weight = pending.cash_weight
            turnover = pending.one_way_turnover
            execution_cost = (
                equity_before
                * turnover
                * self._policy.transaction_cost_bps
                / _BPS
            )
            pending = None
            _append_journal(
                journal,
                timestamp=observation.received_at,
                event_type=ForwardShadowJournalEventType.REBALANCE_EXECUTED,
                reason="pending virtual allocation executed at observation boundary",
                observation_id=observation.observation_id,
            )
        elif pending is not None and pending.execute_sequence < observation.sequence:
            raise ForwardShadowIntegrityError(
                "Pending allocation execution sequence was skipped."
            )

        gross_return = sum(
            (
                weight * observation.returns_by_candidate[candidate_id]
                for candidate_id, weight in active_weights.items()
            ),
            _ZERO,
        )
        equity_after_cost = equity_before - execution_cost
        if equity_after_cost <= _ZERO:
            raise ForwardShadowIntegrityError(
                "Simulated execution cost exhausted portfolio equity."
            )
        equity_after = equity_after_cost * (_ONE + gross_return)
        if equity_after <= _ZERO:
            raise ForwardShadowIntegrityError(
                "Forward-shadow portfolio equity became nonpositive."
            )
        net_return = equity_after / equity_before - _ONE
        peak_equity = max(peak_equity, equity_after)
        drawdown = equity_after / peak_equity - _ONE

        lateness = observation.received_at - observation.observed_at
        is_stale = lateness > timedelta(
            seconds=self._policy.maximum_observation_lateness_seconds
        )
        if is_stale and not feed_stale:
            feed_stale = True
            pending = None
            active_weights = {}
            cash_weight = _ONE
            _append_journal(
                journal,
                timestamp=observation.received_at,
                event_type=ForwardShadowJournalEventType.FEED_STALE,
                reason="observation latency exceeded the fail-closed feed limit",
                observation_id=observation.observation_id,
            )

        if (
            drawdown <= self._policy.maximum_drawdown_before_halt
            and not risk_halted
        ):
            risk_halted = True
            pending = None
            active_weights = {}
            cash_weight = _ONE
            _append_journal(
                journal,
                timestamp=observation.received_at,
                event_type=ForwardShadowJournalEventType.RISK_HALT_TRIGGERED,
                reason="portfolio drawdown reached the fail-closed halt threshold",
                observation_id=observation.observation_id,
            )

        ledger_snapshot = ForwardShadowLedgerSnapshot(
            observation_id=observation.observation_id,
            sequence=observation.sequence,
            timestamp=observation.observed_at,
            equity_before=equity_before,
            execution_cost=execution_cost,
            gross_period_return=gross_return,
            net_period_return=net_return,
            equity_after=equity_after,
            drawdown=drawdown,
            weights=_weight_tuple(active_weights),
            cash_weight=cash_weight,
            one_way_turnover=turnover,
            risk_halted=risk_halted,
            feed_stale=feed_stale,
        )
        ledger = (*state.ledger, ledger_snapshot)

        if (
            not risk_halted
            and not feed_stale
            and observation.sequence
            % self._policy.rebalance_every_observations
            == 0
        ):
            pending = self._schedule_allocation(
                observations,
                observation,
                active_weights,
                cash_weight,
                pending,
                journal,
            )

        return ForwardShadowState(
            dataset_digest=state.dataset_digest,
            manifest_digest=self._manifest.manifest_digest,
            manifest_event_ids=tuple(
                sorted(item.event_id for item in self._manifest.events)
            ),
            policy=state.policy,
            observations=observations,
            journal=tuple(journal),
            ledger=ledger,
            active_weights=_weight_tuple(active_weights),
            cash_weight=cash_weight,
            pending_allocation=pending,
            equity=equity_after,
            peak_equity=peak_equity,
            risk_halted=risk_halted,
            feed_stale=feed_stale,
            visible_event_ids=tuple(sorted(visible_event_ids.items())),
            updated_at=observation.received_at,
        )

    def _validate_sequence_and_time(
        self,
        state: ForwardShadowState,
        observation: ForwardShadowObservation,
    ) -> None:
        last_sequence = state.last_sequence
        if last_sequence is not None and observation.sequence != last_sequence + 1:
            raise ForwardShadowIntegrityError(
                "New observation sequence must follow the current checkpoint."
            )
        if state.observations:
            previous = state.observations[-1]
            if observation.observed_at <= previous.observed_at:
                raise ForwardShadowIntegrityError(
                    "Observation times must increase strictly."
                )
            if observation.received_at < previous.received_at:
                raise ForwardShadowIntegrityError(
                    "Receipt times cannot move backward."
                )
        now = self._clock.now_utc().astimezone(UTC)
        future_limit = now + timedelta(
            seconds=self._policy.maximum_future_clock_skew_seconds
        )
        if observation.observed_at.astimezone(UTC) > future_limit:
            raise ForwardShadowIntegrityError(
                "Observation time is beyond the allowed future clock skew."
            )
        if observation.received_at.astimezone(UTC) > future_limit:
            raise ForwardShadowIntegrityError(
                "Receipt time is beyond the allowed future clock skew."
            )

    def _record_visible_evidence(
        self,
        observation: ForwardShadowObservation,
        visible_event_ids: dict[str, str],
        journal: list[ForwardShadowJournalEntry],
    ) -> None:
        for candidate_id in self._manifest.candidate_ids:
            visible = self._resolver.visible_event(
                candidate_id,
                observation.received_at,
            )
            if (
                visible is None
                or visible_event_ids.get(candidate_id) == visible.event_id
            ):
                continue
            visible_event_ids[candidate_id] = visible.event_id
            _append_journal(
                journal,
                timestamp=observation.received_at,
                event_type=ForwardShadowJournalEventType.EVIDENCE_VISIBLE,
                reason=(
                    f"{visible.state.value} evidence became visible: "
                    f"{visible.reason}"
                ),
                observation_id=observation.observation_id,
                candidate_id=candidate_id,
            )
            event_type = (
                ForwardShadowJournalEventType.CANDIDATE_ELIGIBLE
                if visible.state is ShadowEvidenceState.PROMOTED
                else ForwardShadowJournalEventType.CANDIDATE_INELIGIBLE
            )
            _append_journal(
                journal,
                timestamp=observation.received_at,
                event_type=event_type,
                reason=f"candidate state changed to {visible.state.value}",
                observation_id=observation.observation_id,
                candidate_id=candidate_id,
            )

    def _schedule_allocation(
        self,
        observations: tuple[ForwardShadowObservation, ...],
        observation: ForwardShadowObservation,
        active_weights: Mapping[str, Decimal],
        cash_weight: Decimal,
        pending: PendingForwardAllocation | None,
        journal: list[ForwardShadowJournalEntry],
    ) -> PendingForwardAllocation | None:
        if pending is not None:
            _append_journal(
                journal,
                timestamp=observation.received_at,
                event_type=ForwardShadowJournalEventType.REBALANCE_REJECTED,
                reason="a prior virtual allocation is still pending execution",
                observation_id=observation.observation_id,
            )
            return pending
        eligible = self._resolver.eligible_candidates(
            observation.received_at,
            maximum_age_days=self._policy.maximum_evidence_age_days,
        )
        if len(eligible) < self._policy.minimum_active_candidates:
            _append_journal(
                journal,
                timestamp=observation.received_at,
                event_type=(
                    ForwardShadowJournalEventType.NO_ELIGIBLE_CANDIDATES
                ),
                reason="visible evidence did not satisfy the active-candidate policy",
                observation_id=observation.observation_id,
            )
            return None
        target_weights, target_cash = self._allocate(observations, eligible)
        turnover = _one_way_turnover(
            active_weights,
            cash_weight,
            target_weights,
            target_cash,
        )
        if turnover > self._policy.maximum_one_way_turnover:
            _append_journal(
                journal,
                timestamp=observation.received_at,
                event_type=ForwardShadowJournalEventType.REBALANCE_REJECTED,
                reason="proposed allocation exceeded the one-way turnover limit",
                observation_id=observation.observation_id,
            )
            return None
        scheduled = PendingForwardAllocation(
            execute_sequence=(
                observation.sequence
                + self._policy.execution_delay_observations
            ),
            weights=_weight_tuple(target_weights),
            cash_weight=target_cash,
            one_way_turnover=turnover,
            reason=(
                f"{self._policy.allocation_method.value} allocation scheduled "
                "from information available at receipt time"
            ),
        )
        _append_journal(
            journal,
            timestamp=observation.received_at,
            event_type=ForwardShadowJournalEventType.REBALANCE_SCHEDULED,
            reason=scheduled.reason,
            observation_id=observation.observation_id,
        )
        return scheduled

    def _allocate(
        self,
        observations: tuple[ForwardShadowObservation, ...],
        eligible: tuple[str, ...],
    ) -> tuple[dict[str, Decimal], Decimal]:
        investable = _ONE - self._policy.minimum_cash_weight
        scores = (
            {candidate_id: _ONE for candidate_id in eligible}
            if self._policy.allocation_method is AllocationMethod.EQUAL_WEIGHT
            else self._inverse_volatility_scores(observations, eligible)
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
        observations: tuple[ForwardShadowObservation, ...],
        eligible: tuple[str, ...],
    ) -> dict[str, Decimal]:
        history = observations[-self._policy.volatility_lookback :]
        scores: dict[str, Decimal] = {}
        for candidate_id in eligible:
            values = tuple(
                float(item.returns_by_candidate[candidate_id])
                for item in history
            )
            deviation = statistics.stdev(values) if len(values) >= 2 else 0.0
            scores[candidate_id] = (
                _ONE if deviation <= 0 else Decimal(str(1.0 / deviation))
            )
        return scores


def load_forward_shadow_observations(
    path: Path,
) -> tuple[ForwardShadowObservation, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ForwardShadowConfigurationError(
            f"Unable to read forward-shadow observation file: {path}"
        ) from error
    observations: list[ForwardShadowObservation] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise ForwardShadowConfigurationError(
                f"Observation line {line_number} is not valid JSON."
            ) from error
        observations.append(parse_forward_shadow_observation(raw))
    if not observations:
        raise ForwardShadowConfigurationError(
            "Forward-shadow observation file is empty."
        )
    return tuple(observations)


def decimal_argument(value: str, field_name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ForwardShadowConfigurationError(
            f"{field_name} must be a valid decimal."
        ) from error
    if not parsed.is_finite():
        raise ForwardShadowConfigurationError(
            f"{field_name} must be finite."
        )
    return parsed


def _append_journal(
    journal: list[ForwardShadowJournalEntry],
    *,
    timestamp: datetime,
    event_type: ForwardShadowJournalEventType,
    reason: str,
    observation_id: str | None,
    candidate_id: str | None = None,
) -> None:
    previous_hash = journal[-1].entry_hash if journal else _GENESIS_HASH
    journal.append(
        ForwardShadowJournalEntry(
            sequence=len(journal),
            timestamp=timestamp,
            event_type=event_type,
            reason=reason,
            observation_id=observation_id,
            candidate_id=candidate_id,
            previous_hash=previous_hash,
        )
    )


def _weight_tuple(weights: Mapping[str, Decimal]) -> tuple[ShadowWeight, ...]:
    return tuple(
        ShadowWeight(candidate_id=candidate_id, weight=weight)
        for candidate_id, weight in sorted(weights.items())
        if weight > _ZERO
    )


def _one_way_turnover(
    old_weights: Mapping[str, Decimal],
    old_cash: Decimal,
    new_weights: Mapping[str, Decimal],
    new_cash: Decimal,
) -> Decimal:
    candidate_ids = set(old_weights) | set(new_weights)
    total_change = sum(
        (
            abs(
                new_weights.get(candidate_id, _ZERO)
                - old_weights.get(candidate_id, _ZERO)
            )
            for candidate_id in candidate_ids
        ),
        abs(new_cash - old_cash),
    )
    return total_change / Decimal("2")


def _capped_weights(
    scores: Mapping[str, Decimal],
    *,
    investable: Decimal,
    maximum_candidate_weight: Decimal,
) -> dict[str, Decimal]:
    if not scores:
        return {}
    weights = {candidate_id: _ZERO for candidate_id in scores}
    remaining = investable
    uncapped = set(scores)
    while uncapped and remaining > _ZERO:
        score_total = sum((scores[item] for item in uncapped), _ZERO)
        if score_total <= _ZERO:
            equal = remaining / Decimal(len(uncapped))
            proposed = {item: equal for item in uncapped}
        else:
            proposed = {
                item: remaining * scores[item] / score_total
                for item in uncapped
            }
        capped = {
            item
            for item, weight in proposed.items()
            if weight > maximum_candidate_weight
        }
        if not capped:
            for item, weight in proposed.items():
                weights[item] += weight
            remaining = _ZERO
            break
        for item in sorted(capped):
            allowance = maximum_candidate_weight - weights[item]
            if allowance > _ZERO:
                weights[item] += allowance
                remaining -= allowance
            uncapped.remove(item)
    return weights
