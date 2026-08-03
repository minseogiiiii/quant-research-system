from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid5

from world_quant_system.research.historical_shadow_models import (
    ShadowWeight,
    parse_utc_datetime,
)
from world_quant_system.research.portfolio_promotion_models import (
    AllocationMethod,
    canonical_json_bytes,
    format_utc,
)

_SCHEMA_VERSION: Final[int] = 1
_REPORT_NAMESPACE: Final[UUID] = UUID("61cc2c8e-a7e5-56ee-891e-b90af79d5869")
_GENESIS_HASH: Final[str] = "0" * 64
_ZERO = Decimal("0")
_ONE = Decimal("1")


class ForwardShadowError(Exception):
    """Base exception for forward-shadow research failures."""


class ForwardShadowConfigurationError(ForwardShadowError):
    """Raised when forward-shadow configuration or input is invalid."""


class ForwardShadowIntegrityError(ForwardShadowError):
    """Raised when append-only forward-shadow state is inconsistent."""


class ForwardShadowEligibilityError(ForwardShadowError):
    """Raised when there is insufficient evidence for forward shadowing."""


class ForwardShadowJournalEventType(StrEnum):
    OBSERVATION_ACCEPTED = "observation_accepted"
    EVIDENCE_VISIBLE = "evidence_visible"
    CANDIDATE_ELIGIBLE = "candidate_eligible"
    CANDIDATE_INELIGIBLE = "candidate_ineligible"
    REBALANCE_SCHEDULED = "rebalance_scheduled"
    REBALANCE_EXECUTED = "rebalance_executed"
    REBALANCE_REJECTED = "rebalance_rejected"
    FEED_STALE = "feed_stale"
    RISK_HALT_TRIGGERED = "risk_halt_triggered"
    NO_ELIGIBLE_CANDIDATES = "no_eligible_candidates"
    IDEMPOTENT_REPLAY = "idempotent_replay"


class ForwardShadowRunDecision(StrEnum):
    RUNNING = "running"
    HALTED = "halted"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True, slots=True)
class ForwardShadowReturn:
    candidate_id: str
    value: Decimal

    def __post_init__(self) -> None:
        _require_nonblank(self.candidate_id, "Candidate ID")
        _require_finite_decimal(self.value, "Candidate return")
        if self.value <= -_ONE:
            raise ForwardShadowConfigurationError(
                "Candidate returns must be greater than -1."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "value": _decimal_text(self.value),
        }


@dataclass(frozen=True, slots=True)
class ForwardShadowObservation:
    sequence: int
    observed_at: datetime
    received_at: datetime
    returns: tuple[ForwardShadowReturn, ...]
    source_digest: str
    observation_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_nonnegative_int(self.sequence, "Observation sequence")
        _require_aware(self.observed_at, "Observation time")
        _require_aware(self.received_at, "Receipt time")
        if self.received_at < self.observed_at:
            raise ForwardShadowConfigurationError(
                "Receipt time cannot precede observation time."
            )
        if not self.returns:
            raise ForwardShadowEligibilityError(
                "Forward-shadow observations require candidate returns."
            )
        candidate_ids = tuple(item.candidate_id for item in self.returns)
        if candidate_ids != tuple(sorted(candidate_ids)):
            raise ForwardShadowConfigurationError(
                "Observation returns must be sorted by candidate ID."
            )
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ForwardShadowIntegrityError(
                "Observation returns cannot repeat a candidate."
            )
        _require_sha256(self.source_digest, "Observation source digest")
        identity = {
            "sequence": self.sequence,
            "observed_at": format_utc(self.observed_at),
            "received_at": format_utc(self.received_at),
            "returns": [item.to_document() for item in self.returns],
            "source_digest": self.source_digest,
        }
        object.__setattr__(
            self,
            "observation_id",
            hashlib.sha256(canonical_json_bytes(identity)).hexdigest(),
        )

    @property
    def returns_by_candidate(self) -> dict[str, Decimal]:
        return {item.candidate_id: item.value for item in self.returns}

    def to_document(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "sequence": self.sequence,
            "observed_at": format_utc(self.observed_at),
            "received_at": format_utc(self.received_at),
            "returns": [item.to_document() for item in self.returns],
            "source_digest": self.source_digest,
        }


@dataclass(frozen=True, slots=True)
class PendingForwardAllocation:
    execute_sequence: int
    weights: tuple[ShadowWeight, ...]
    cash_weight: Decimal
    one_way_turnover: Decimal
    reason: str

    def __post_init__(self) -> None:
        _require_nonnegative_int(self.execute_sequence, "Execution sequence")
        _validate_weights(self.weights, self.cash_weight)
        _require_nonnegative_decimal(
            self.one_way_turnover,
            "Pending one-way turnover",
        )
        _require_nonblank(self.reason, "Pending allocation reason")

    def to_document(self) -> dict[str, object]:
        return {
            "execute_sequence": self.execute_sequence,
            "weights": [item.to_document() for item in self.weights],
            "cash_weight": _decimal_text(self.cash_weight),
            "one_way_turnover": _decimal_text(self.one_way_turnover),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ForwardShadowJournalEntry:
    sequence: int
    timestamp: datetime
    event_type: ForwardShadowJournalEventType
    reason: str
    observation_id: str | None
    candidate_id: str | None
    previous_hash: str
    entry_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_nonnegative_int(self.sequence, "Journal sequence")
        _require_aware(self.timestamp, "Journal timestamp")
        if not isinstance(self.event_type, ForwardShadowJournalEventType):
            raise ForwardShadowConfigurationError(
                "Journal event type is invalid."
            )
        _require_nonblank(self.reason, "Journal reason")
        if self.observation_id is not None:
            _require_sha256(self.observation_id, "Journal observation ID")
        if self.candidate_id is not None:
            _require_nonblank(self.candidate_id, "Journal candidate ID")
        _require_sha256(self.previous_hash, "Previous journal hash")
        identity = {
            "sequence": self.sequence,
            "timestamp": format_utc(self.timestamp),
            "event_type": self.event_type.value,
            "reason": self.reason,
            "observation_id": self.observation_id,
            "candidate_id": self.candidate_id,
            "previous_hash": self.previous_hash,
        }
        object.__setattr__(
            self,
            "entry_hash",
            hashlib.sha256(canonical_json_bytes(identity)).hexdigest(),
        )

    def to_document(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "timestamp": format_utc(self.timestamp),
            "event_type": self.event_type.value,
            "reason": self.reason,
            "observation_id": self.observation_id,
            "candidate_id": self.candidate_id,
            "previous_hash": self.previous_hash,
            "entry_hash": self.entry_hash,
        }


@dataclass(frozen=True, slots=True)
class ForwardShadowLedgerSnapshot:
    observation_id: str
    sequence: int
    timestamp: datetime
    equity_before: Decimal
    execution_cost: Decimal
    gross_period_return: Decimal
    net_period_return: Decimal
    equity_after: Decimal
    drawdown: Decimal
    weights: tuple[ShadowWeight, ...]
    cash_weight: Decimal
    one_way_turnover: Decimal
    risk_halted: bool
    feed_stale: bool

    def __post_init__(self) -> None:
        _require_sha256(self.observation_id, "Ledger observation ID")
        _require_nonnegative_int(self.sequence, "Ledger sequence")
        _require_aware(self.timestamp, "Ledger timestamp")
        _require_positive_decimal(self.equity_before, "Equity before")
        _require_nonnegative_decimal(self.execution_cost, "Execution cost")
        _require_finite_decimal(self.gross_period_return, "Gross period return")
        _require_finite_decimal(self.net_period_return, "Net period return")
        _require_positive_decimal(self.equity_after, "Equity after")
        _require_finite_decimal(self.drawdown, "Drawdown")
        if self.drawdown > _ZERO:
            raise ForwardShadowConfigurationError(
                "Drawdown cannot be positive."
            )
        _validate_weights(self.weights, self.cash_weight)
        _require_nonnegative_decimal(
            self.one_way_turnover,
            "One-way turnover",
        )
        for boolean_value, field_name in (
            (self.risk_halted, "Risk-halted state"),
            (self.feed_stale, "Feed-stale state"),
        ):
            if not isinstance(boolean_value, bool):
                raise ForwardShadowConfigurationError(
                    f"{field_name} must be boolean."
                )

    def to_document(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "sequence": self.sequence,
            "timestamp": format_utc(self.timestamp),
            "equity_before": _decimal_text(self.equity_before),
            "execution_cost": _decimal_text(self.execution_cost),
            "gross_period_return": _decimal_text(self.gross_period_return),
            "net_period_return": _decimal_text(self.net_period_return),
            "equity_after": _decimal_text(self.equity_after),
            "drawdown": _decimal_text(self.drawdown),
            "weights": [item.to_document() for item in self.weights],
            "cash_weight": _decimal_text(self.cash_weight),
            "one_way_turnover": _decimal_text(self.one_way_turnover),
            "risk_halted": self.risk_halted,
            "feed_stale": self.feed_stale,
        }


@dataclass(frozen=True, slots=True)
class ForwardShadowPolicy:
    initial_equity: Decimal = Decimal("1000000")
    allocation_method: AllocationMethod = AllocationMethod.EQUAL_WEIGHT
    rebalance_every_observations: int = 5
    volatility_lookback: int = 20
    execution_delay_observations: int = 1
    minimum_active_candidates: int = 2
    minimum_cash_weight: Decimal = Decimal("0.05")
    maximum_candidate_weight: Decimal = Decimal("0.40")
    maximum_one_way_turnover: Decimal = Decimal("1")
    transaction_cost_bps: Decimal = Decimal("10")
    maximum_evidence_age_days: int = 120
    maximum_observation_lateness_seconds: int = 900
    maximum_future_clock_skew_seconds: int = 5
    maximum_drawdown_before_halt: Decimal = Decimal("-0.30")

    def __post_init__(self) -> None:
        _require_positive_decimal(self.initial_equity, "Initial equity")
        if not isinstance(self.allocation_method, AllocationMethod):
            raise ForwardShadowConfigurationError(
                "Allocation method must be an AllocationMethod."
            )
        for integer_value, field_name in (
            (
                self.rebalance_every_observations,
                "Rebalance observation frequency",
            ),
            (self.volatility_lookback, "Volatility lookback"),
            (
                self.execution_delay_observations,
                "Execution delay observations",
            ),
            (self.minimum_active_candidates, "Minimum active candidates"),
            (self.maximum_evidence_age_days, "Maximum evidence age days"),
            (
                self.maximum_observation_lateness_seconds,
                "Maximum observation lateness seconds",
            ),
        ):
            _require_positive_int(integer_value, field_name)
        _require_nonnegative_int(
            self.maximum_future_clock_skew_seconds,
            "Maximum future clock-skew seconds",
        )
        for unit_value, field_name in (
            (self.minimum_cash_weight, "Minimum cash weight"),
            (self.maximum_candidate_weight, "Maximum candidate weight"),
        ):
            _require_unit_decimal(unit_value, field_name)
        if self.minimum_cash_weight >= _ONE:
            raise ForwardShadowConfigurationError(
                "Minimum cash weight must be less than one."
            )
        if self.maximum_candidate_weight <= _ZERO:
            raise ForwardShadowConfigurationError(
                "Maximum candidate weight must be positive."
            )
        for nonnegative_value, field_name in (
            (
                self.maximum_one_way_turnover,
                "Maximum one-way turnover",
            ),
            (self.transaction_cost_bps, "Transaction cost bps"),
        ):
            _require_nonnegative_decimal(nonnegative_value, field_name)
        _require_finite_decimal(
            self.maximum_drawdown_before_halt,
            "Maximum drawdown before halt",
        )
        if self.maximum_drawdown_before_halt >= _ZERO:
            raise ForwardShadowConfigurationError(
                "Drawdown halt threshold must be negative."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "initial_equity": _decimal_text(self.initial_equity),
            "allocation_method": self.allocation_method.value,
            "rebalance_every_observations": self.rebalance_every_observations,
            "volatility_lookback": self.volatility_lookback,
            "execution_delay_observations": (
                self.execution_delay_observations
            ),
            "minimum_active_candidates": self.minimum_active_candidates,
            "minimum_cash_weight": _decimal_text(self.minimum_cash_weight),
            "maximum_candidate_weight": _decimal_text(
                self.maximum_candidate_weight
            ),
            "maximum_one_way_turnover": _decimal_text(
                self.maximum_one_way_turnover
            ),
            "transaction_cost_bps": _decimal_text(self.transaction_cost_bps),
            "maximum_evidence_age_days": self.maximum_evidence_age_days,
            "maximum_observation_lateness_seconds": (
                self.maximum_observation_lateness_seconds
            ),
            "maximum_future_clock_skew_seconds": (
                self.maximum_future_clock_skew_seconds
            ),
            "maximum_drawdown_before_halt": _decimal_text(
                self.maximum_drawdown_before_halt
            ),
        }


@dataclass(frozen=True, slots=True)
class ForwardShadowState:
    dataset_digest: str
    manifest_digest: str
    manifest_event_ids: tuple[str, ...]
    policy: ForwardShadowPolicy
    observations: tuple[ForwardShadowObservation, ...]
    journal: tuple[ForwardShadowJournalEntry, ...]
    ledger: tuple[ForwardShadowLedgerSnapshot, ...]
    active_weights: tuple[ShadowWeight, ...]
    cash_weight: Decimal
    pending_allocation: PendingForwardAllocation | None
    equity: Decimal
    peak_equity: Decimal
    risk_halted: bool
    feed_stale: bool
    visible_event_ids: tuple[tuple[str, str], ...]
    updated_at: datetime | None
    state_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.dataset_digest, "Dataset digest")
        _require_sha256(self.manifest_digest, "Manifest digest")
        if tuple(sorted(self.manifest_event_ids)) != self.manifest_event_ids:
            raise ForwardShadowIntegrityError(
                "Manifest event IDs must be sorted."
            )
        if len(set(self.manifest_event_ids)) != len(self.manifest_event_ids):
            raise ForwardShadowIntegrityError(
                "Manifest event IDs cannot contain duplicates."
            )
        for event_id in self.manifest_event_ids:
            _require_sha256(event_id, "Manifest event ID")
        if not isinstance(self.policy, ForwardShadowPolicy):
            raise ForwardShadowConfigurationError(
                "Forward-shadow state policy is invalid."
            )
        sequences = tuple(item.sequence for item in self.observations)
        if sequences and sequences != tuple(
            range(sequences[0], sequences[0] + len(sequences))
        ):
            raise ForwardShadowIntegrityError(
                "Observation sequences must be contiguous."
            )
        if len({item.observation_id for item in self.observations}) != len(
            self.observations
        ):
            raise ForwardShadowIntegrityError(
                "State cannot contain duplicate observation IDs."
            )
        if tuple(item.sequence for item in self.journal) != tuple(
            range(len(self.journal))
        ):
            raise ForwardShadowIntegrityError(
                "Journal sequences must be contiguous from zero."
            )
        previous_hash = _GENESIS_HASH
        for entry in self.journal:
            if entry.previous_hash != previous_hash:
                raise ForwardShadowIntegrityError(
                    "Journal hash chain is broken."
                )
            previous_hash = entry.entry_hash
        if tuple(item.sequence for item in self.ledger) != sequences:
            raise ForwardShadowIntegrityError(
                "Ledger and observation sequences must agree."
            )
        _validate_weights(self.active_weights, self.cash_weight)
        _require_positive_decimal(self.equity, "Current equity")
        _require_positive_decimal(self.peak_equity, "Peak equity")
        if self.peak_equity < self.equity:
            raise ForwardShadowIntegrityError(
                "Peak equity cannot be below current equity."
            )
        for boolean_value, field_name in (
            (self.risk_halted, "Risk-halted state"),
            (self.feed_stale, "Feed-stale state"),
        ):
            if not isinstance(boolean_value, bool):
                raise ForwardShadowConfigurationError(
                    f"{field_name} must be boolean."
                )
        if tuple(sorted(self.visible_event_ids)) != self.visible_event_ids:
            raise ForwardShadowIntegrityError(
                "Visible event IDs must be sorted by candidate ID."
            )
        if len({item[0] for item in self.visible_event_ids}) != len(
            self.visible_event_ids
        ):
            raise ForwardShadowIntegrityError(
                "Visible event IDs cannot repeat candidates."
            )
        for candidate_id, event_id in self.visible_event_ids:
            _require_nonblank(candidate_id, "Visible candidate ID")
            _require_sha256(event_id, "Visible event ID")
        if self.updated_at is not None:
            _require_aware(self.updated_at, "State update time")
        if self.observations:
            expected_updated = self.observations[-1].received_at.astimezone(UTC)
            if (
                self.updated_at is None
                or self.updated_at.astimezone(UTC) != expected_updated
            ):
                raise ForwardShadowIntegrityError(
                    "State update time must equal the latest receipt time."
                )
        elif self.updated_at is not None:
            raise ForwardShadowIntegrityError(
                "Empty state cannot have an update time."
            )
        object.__setattr__(
            self,
            "state_digest",
            hashlib.sha256(
                canonical_json_bytes(self.identity_document())
            ).hexdigest(),
        )

    @property
    def last_sequence(self) -> int | None:
        return self.observations[-1].sequence if self.observations else None

    @property
    def observation_ids(self) -> frozenset[str]:
        return frozenset(item.observation_id for item in self.observations)

    @property
    def visible_event_map(self) -> dict[str, str]:
        return dict(self.visible_event_ids)

    @property
    def decision(self) -> ForwardShadowRunDecision:
        if self.risk_halted or self.feed_stale:
            return ForwardShadowRunDecision.HALTED
        if not self.active_weights and self.pending_allocation is None:
            return ForwardShadowRunDecision.INSUFFICIENT_EVIDENCE
        return ForwardShadowRunDecision.RUNNING

    def identity_document(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "execution_mode": "forward_shadow",
            "network_access": "disabled",
            "broker_provider": "none",
            "live_trading": "disabled",
            "order_submission": "disabled",
            "dataset_digest": self.dataset_digest,
            "manifest_digest": self.manifest_digest,
            "manifest_event_ids": list(self.manifest_event_ids),
            "policy": self.policy.to_document(),
            "observations": [item.to_document() for item in self.observations],
            "journal": [item.to_document() for item in self.journal],
            "ledger": [item.to_document() for item in self.ledger],
            "active_weights": [
                item.to_document() for item in self.active_weights
            ],
            "cash_weight": _decimal_text(self.cash_weight),
            "pending_allocation": (
                None
                if self.pending_allocation is None
                else self.pending_allocation.to_document()
            ),
            "equity": _decimal_text(self.equity),
            "peak_equity": _decimal_text(self.peak_equity),
            "risk_halted": self.risk_halted,
            "feed_stale": self.feed_stale,
            "visible_event_ids": [list(item) for item in self.visible_event_ids],
            "updated_at": (
                None if self.updated_at is None else format_utc(self.updated_at)
            ),
        }

    def to_document(self) -> dict[str, object]:
        document = self.identity_document()
        document["state_digest"] = self.state_digest
        document["decision"] = self.decision.value
        return document


@dataclass(frozen=True, slots=True)
class ForwardShadowBatchReport:
    state: ForwardShadowState
    processed_observations: int
    duplicate_observations: int
    generated_at: datetime
    report_id: str = field(init=False)
    report_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.state, ForwardShadowState):
            raise ForwardShadowConfigurationError(
                "Batch report requires a ForwardShadowState."
            )
        _require_nonnegative_int(
            self.processed_observations,
            "Processed observation count",
        )
        _require_nonnegative_int(
            self.duplicate_observations,
            "Duplicate observation count",
        )
        _require_aware(self.generated_at, "Report generation time")
        identity = self.identity_document()
        report_digest = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
        object.__setattr__(self, "report_digest", report_digest)
        object.__setattr__(
            self,
            "report_id",
            str(uuid5(_REPORT_NAMESPACE, report_digest)),
        )

    def identity_document(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "state_digest": self.state.state_digest,
            "processed_observations": self.processed_observations,
            "duplicate_observations": self.duplicate_observations,
        }

    def to_document(self) -> dict[str, object]:
        document = self.identity_document()
        document.update(
            {
                "generated_at": format_utc(self.generated_at),
                "report_id": self.report_id,
                "report_digest": self.report_digest,
                "state": self.state.to_document(),
            }
        )
        return document


def parse_forward_shadow_observation(raw: object) -> ForwardShadowObservation:
    if not isinstance(raw, dict):
        raise ForwardShadowConfigurationError(
            "Observation JSON value must be an object."
        )
    sequence = raw.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool):
        raise ForwardShadowConfigurationError(
            "Observation sequence must be an integer."
        )
    raw_returns = raw.get("returns")
    if not isinstance(raw_returns, dict):
        raise ForwardShadowConfigurationError(
            "Observation returns must be a JSON object."
        )
    returns = tuple(
        ForwardShadowReturn(
            candidate_id=candidate_id,
            value=_parse_decimal(raw_value, f"return for {candidate_id}"),
        )
        for candidate_id, raw_value in sorted(raw_returns.items())
        if isinstance(candidate_id, str)
    )
    if len(returns) != len(raw_returns):
        raise ForwardShadowConfigurationError(
            "Observation return keys must be strings."
        )
    source_digest = raw.get("source_digest")
    if not isinstance(source_digest, str):
        raise ForwardShadowConfigurationError(
            "Observation source_digest must be a string."
        )
    return ForwardShadowObservation(
        sequence=sequence,
        observed_at=parse_utc_datetime(raw.get("observed_at"), "observed_at"),
        received_at=parse_utc_datetime(raw.get("received_at"), "received_at"),
        returns=returns,
        source_digest=source_digest,
    )


def _parse_decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ForwardShadowConfigurationError(
            f"{field_name} must be a decimal-compatible value."
        )
    try:
        parsed = Decimal(str(value))
    except ArithmeticError as error:
        raise ForwardShadowConfigurationError(
            f"{field_name} is not a valid decimal."
        ) from error
    _require_finite_decimal(parsed, field_name)
    return parsed


def _validate_weights(
    weights: tuple[ShadowWeight, ...],
    cash_weight: Decimal,
) -> None:
    candidate_ids = tuple(item.candidate_id for item in weights)
    if candidate_ids != tuple(sorted(candidate_ids)):
        raise ForwardShadowConfigurationError(
            "Weights must be sorted by candidate ID."
        )
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ForwardShadowIntegrityError(
            "Weights cannot repeat candidate IDs."
        )
    _require_unit_decimal(cash_weight, "Cash weight")
    total = sum((item.weight for item in weights), cash_weight)
    if abs(total - _ONE) > Decimal("0.000000000001"):
        raise ForwardShadowConfigurationError(
            "Weights and cash must sum to one."
        )


def _decimal_text(value: Decimal) -> str:
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _require_nonblank(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ForwardShadowConfigurationError(
            f"{field_name} must be a nonblank string."
        )


def _require_sha256(value: str, field_name: str) -> None:
    _require_nonblank(value, field_name)
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ForwardShadowConfigurationError(
            f"{field_name} must be a lowercase SHA-256 digest."
        )


def _require_aware(value: datetime, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ForwardShadowConfigurationError(
            f"{field_name} must be timezone-aware."
        )


def _require_finite_decimal(value: Decimal, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ForwardShadowConfigurationError(
            f"{field_name} must be a finite Decimal."
        )


def _require_positive_decimal(value: Decimal, field_name: str) -> None:
    _require_finite_decimal(value, field_name)
    if value <= _ZERO:
        raise ForwardShadowConfigurationError(
            f"{field_name} must be positive."
        )


def _require_nonnegative_decimal(value: Decimal, field_name: str) -> None:
    _require_finite_decimal(value, field_name)
    if value < _ZERO:
        raise ForwardShadowConfigurationError(
            f"{field_name} cannot be negative."
        )


def _require_unit_decimal(value: Decimal, field_name: str) -> None:
    _require_finite_decimal(value, field_name)
    if value < _ZERO or value > _ONE:
        raise ForwardShadowConfigurationError(
            f"{field_name} must be between zero and one."
        )


def _require_positive_int(value: int, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ForwardShadowConfigurationError(
            f"{field_name} must be a positive integer."
        )


def _require_nonnegative_int(value: int, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ForwardShadowConfigurationError(
            f"{field_name} must be a nonnegative integer."
        )
