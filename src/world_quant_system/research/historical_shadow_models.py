from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid5

from world_quant_system.research.portfolio_promotion_models import (
    AllocationMethod,
    canonical_json_bytes,
    format_utc,
)

_SCHEMA_VERSION: Final[int] = 1
_REPORT_NAMESPACE: Final[UUID] = UUID("31bf7e04-bd9a-5f2a-a11f-0a287a748a61")
_ZERO = Decimal("0")
_ONE = Decimal("1")


class HistoricalShadowError(Exception):
    """Base exception for historical shadow research failures."""


class HistoricalShadowConfigurationError(HistoricalShadowError):
    """Raised when a manifest, policy, or input is invalid."""


class HistoricalShadowIntegrityError(HistoricalShadowError):
    """Raised when immutable point-in-time evidence is inconsistent."""


class HistoricalShadowEligibilityError(HistoricalShadowError):
    """Raised when valid evidence is insufficient for simulation."""


class ShadowEvidenceState(StrEnum):
    PROMOTED = "promoted"
    SUSPENDED = "suspended"
    RETIRED = "retired"


class ShadowJournalEventType(StrEnum):
    EVIDENCE_VISIBLE = "evidence_visible"
    CANDIDATE_ELIGIBLE = "candidate_eligible"
    CANDIDATE_INELIGIBLE = "candidate_ineligible"
    REBALANCE_SCHEDULED = "rebalance_scheduled"
    REBALANCE_EXECUTED = "rebalance_executed"
    REBALANCE_REJECTED = "rebalance_rejected"
    RISK_HALT_TRIGGERED = "risk_halt_triggered"
    NO_ELIGIBLE_CANDIDATES = "no_eligible_candidates"


class ShadowScenarioName(StrEnum):
    BASE = "base"
    EQUAL_WEIGHT_BASELINE = "equal_weight_baseline"
    DOUBLE_COST = "double_cost"
    EXTRA_EXECUTION_DELAY = "extra_execution_delay"
    LARGEST_CANDIDATE_REMOVED = "largest_candidate_removed"


class ShadowRunDecision(StrEnum):
    READY_FOR_FORWARD_SHADOW_RESEARCH = "ready_for_forward_shadow_research"
    REJECTED = "rejected"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True, slots=True)
class ShadowEvidenceEvent:
    candidate_id: str
    strategy_name: str
    strategy_version: str
    state: ShadowEvidenceState
    effective_at: datetime
    available_at: datetime
    evidence_digest: str
    reason: str
    event_id: str = field(init=False)

    def __post_init__(self) -> None:
        for text_value, field_name in (
            (self.candidate_id, "Candidate ID"),
            (self.strategy_name, "Strategy name"),
            (self.strategy_version, "Strategy version"),
            (self.reason, "Evidence reason"),
        ):
            _require_nonblank(text_value, field_name)
        if not isinstance(self.state, ShadowEvidenceState):
            raise HistoricalShadowConfigurationError(
                "Evidence state must be a ShadowEvidenceState."
            )
        _require_aware(self.effective_at, "Evidence effective time")
        _require_aware(self.available_at, "Evidence availability time")
        if self.available_at < self.effective_at:
            raise HistoricalShadowConfigurationError(
                "Evidence cannot be available before it becomes effective."
            )
        _require_sha256(self.evidence_digest, "Evidence digest")
        identity = {
            "candidate_id": self.candidate_id,
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "state": self.state.value,
            "effective_at": format_utc(self.effective_at),
            "available_at": format_utc(self.available_at),
            "evidence_digest": self.evidence_digest,
            "reason": self.reason,
        }
        object.__setattr__(
            self,
            "event_id",
            hashlib.sha256(canonical_json_bytes(identity)).hexdigest(),
        )

    def to_document(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "candidate_id": self.candidate_id,
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "state": self.state.value,
            "effective_at": format_utc(self.effective_at),
            "available_at": format_utc(self.available_at),
            "evidence_digest": self.evidence_digest,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ShadowEvidenceManifest:
    dataset_digest: str
    return_matrix_digest: str
    events: tuple[ShadowEvidenceEvent, ...]
    manifest_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.dataset_digest, "Dataset digest")
        _require_sha256(self.return_matrix_digest, "Return matrix digest")
        if not self.events:
            raise HistoricalShadowEligibilityError(
                "Historical shadow evidence manifest cannot be empty."
            )
        ordered = tuple(
            sorted(
                self.events,
                key=lambda item: (
                    item.available_at.astimezone(UTC),
                    item.candidate_id,
                    item.event_id,
                ),
            )
        )
        if ordered != self.events:
            raise HistoricalShadowConfigurationError(
                "Evidence events must be sorted by availability and identity."
            )
        if len({item.event_id for item in self.events}) != len(self.events):
            raise HistoricalShadowIntegrityError(
                "Evidence manifest cannot contain duplicate events."
            )
        previous_by_candidate: dict[str, datetime] = {}
        for evidence_event in self.events:
            previous = previous_by_candidate.get(evidence_event.candidate_id)
            normalized = evidence_event.available_at.astimezone(UTC)
            if previous is not None and normalized <= previous:
                raise HistoricalShadowConfigurationError(
                    "Each candidate's evidence availability must increase strictly."
                )
            previous_by_candidate[evidence_event.candidate_id] = normalized
        object.__setattr__(
            self,
            "manifest_digest",
            hashlib.sha256(
                canonical_json_bytes(self.identity_document())
            ).hexdigest(),
        )

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(sorted({item.candidate_id for item in self.events}))

    def identity_document(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "dataset_digest": self.dataset_digest,
            "return_matrix_digest": self.return_matrix_digest,
            "events": [item.to_document() for item in self.events],
        }

    def to_document(self) -> dict[str, object]:
        document = self.identity_document()
        document["manifest_digest"] = self.manifest_digest
        return document


@dataclass(frozen=True, slots=True)
class HistoricalShadowPolicy:
    initial_equity: Decimal = Decimal("1000000")
    annualization_periods: int = 252
    allocation_method: AllocationMethod = AllocationMethod.EQUAL_WEIGHT
    rebalance_frequency: int = 5
    volatility_lookback: int = 20
    execution_delay_periods: int = 1
    minimum_active_candidates: int = 2
    minimum_cash_weight: Decimal = Decimal("0.05")
    maximum_candidate_weight: Decimal = Decimal("0.40")
    maximum_one_way_turnover: Decimal = Decimal("1")
    transaction_cost_bps: Decimal = Decimal("10")
    maximum_evidence_age_days: int = 120
    maximum_drawdown_before_halt: Decimal = Decimal("-0.30")
    minimum_observations: int = 60
    minimum_base_cumulative_return: Decimal = Decimal("0")
    minimum_stress_cumulative_return: Decimal = Decimal("-0.05")
    maximum_stress_drawdown: Decimal = Decimal("-0.40")
    expected_shortfall_fraction: Decimal = Decimal("0.05")

    def __post_init__(self) -> None:
        _require_positive_decimal(self.initial_equity, "Initial equity")
        for integer_value, field_name in (
            (self.annualization_periods, "Annualization periods"),
            (self.rebalance_frequency, "Rebalance frequency"),
            (self.volatility_lookback, "Volatility lookback"),
            (self.execution_delay_periods, "Execution delay periods"),
            (self.minimum_active_candidates, "Minimum active candidates"),
            (self.maximum_evidence_age_days, "Maximum evidence age days"),
            (self.minimum_observations, "Minimum observations"),
        ):
            _require_positive_int(integer_value, field_name)
        if not isinstance(self.allocation_method, AllocationMethod):
            raise HistoricalShadowConfigurationError(
                "Allocation method must be an AllocationMethod."
            )
        for unit_value, field_name in (
            (self.minimum_cash_weight, "Minimum cash weight"),
            (self.maximum_candidate_weight, "Maximum candidate weight"),
            (
                self.expected_shortfall_fraction,
                "Expected-shortfall fraction",
            ),
        ):
            _require_open_closed_unit_decimal(unit_value, field_name)
        if self.minimum_cash_weight >= _ONE:
            raise HistoricalShadowConfigurationError(
                "Minimum cash weight must be below one."
            )
        if self.maximum_candidate_weight > (_ONE - self.minimum_cash_weight):
            raise HistoricalShadowConfigurationError(
                "Candidate weight cap cannot exceed investable capital."
            )
        _require_open_closed_unit_decimal(
            self.maximum_one_way_turnover,
            "Maximum one-way turnover",
        )
        _require_nonnegative_decimal(
            self.transaction_cost_bps,
            "Transaction cost bps",
        )
        for return_value, field_name in (
            (
                self.minimum_base_cumulative_return,
                "Minimum base cumulative return",
            ),
            (
                self.minimum_stress_cumulative_return,
                "Minimum stress cumulative return",
            ),
            (
                self.maximum_drawdown_before_halt,
                "Maximum drawdown before halt",
            ),
            (self.maximum_stress_drawdown, "Maximum stress drawdown"),
        ):
            _require_finite_decimal(return_value, field_name)
        if self.maximum_drawdown_before_halt >= _ZERO:
            raise HistoricalShadowConfigurationError(
                "Drawdown halt threshold must be negative."
            )
        if self.maximum_stress_drawdown >= _ZERO:
            raise HistoricalShadowConfigurationError(
                "Maximum stress drawdown must be negative."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "initial_equity": _decimal_text(self.initial_equity),
            "annualization_periods": self.annualization_periods,
            "allocation_method": self.allocation_method.value,
            "rebalance_frequency": self.rebalance_frequency,
            "volatility_lookback": self.volatility_lookback,
            "execution_delay_periods": self.execution_delay_periods,
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
            "maximum_drawdown_before_halt": _decimal_text(
                self.maximum_drawdown_before_halt
            ),
            "minimum_observations": self.minimum_observations,
            "minimum_base_cumulative_return": _decimal_text(
                self.minimum_base_cumulative_return
            ),
            "minimum_stress_cumulative_return": _decimal_text(
                self.minimum_stress_cumulative_return
            ),
            "maximum_stress_drawdown": _decimal_text(
                self.maximum_stress_drawdown
            ),
            "expected_shortfall_fraction": _decimal_text(
                self.expected_shortfall_fraction
            ),
        }


@dataclass(frozen=True, slots=True)
class ShadowWeight:
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
class ShadowJournalEntry:
    sequence: int
    timestamp: datetime
    event_type: ShadowJournalEventType
    candidate_id: str | None
    reason: str
    related_event_id: str | None = None
    entry_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_nonnegative_int(self.sequence, "Journal sequence")
        _require_aware(self.timestamp, "Journal timestamp")
        if not isinstance(self.event_type, ShadowJournalEventType):
            raise HistoricalShadowConfigurationError(
                "Journal event type must be a ShadowJournalEventType."
            )
        if self.candidate_id is not None:
            _require_nonblank(self.candidate_id, "Journal candidate ID")
        _require_nonblank(self.reason, "Journal reason")
        if self.related_event_id is not None:
            _require_sha256(self.related_event_id, "Related event ID")
        identity = {
            "sequence": self.sequence,
            "timestamp": format_utc(self.timestamp),
            "event_type": self.event_type.value,
            "candidate_id": self.candidate_id,
            "reason": self.reason,
            "related_event_id": self.related_event_id,
        }
        object.__setattr__(
            self,
            "entry_id",
            hashlib.sha256(canonical_json_bytes(identity)).hexdigest(),
        )

    def to_document(self) -> dict[str, object]:
        return {
            "entry_id": self.entry_id,
            "sequence": self.sequence,
            "timestamp": format_utc(self.timestamp),
            "event_type": self.event_type.value,
            "candidate_id": self.candidate_id,
            "reason": self.reason,
            "related_event_id": self.related_event_id,
        }


@dataclass(frozen=True, slots=True)
class ShadowLedgerSnapshot:
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

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "Ledger timestamp")
        _require_positive_decimal(self.equity_before, "Equity before")
        _require_nonnegative_decimal(self.execution_cost, "Execution cost")
        _require_finite_decimal(self.gross_period_return, "Gross period return")
        _require_finite_decimal(self.net_period_return, "Net period return")
        _require_positive_decimal(self.equity_after, "Equity after")
        _require_finite_decimal(self.drawdown, "Drawdown")
        if self.drawdown > _ZERO:
            raise HistoricalShadowConfigurationError(
                "Drawdown cannot be positive."
            )
        if tuple(item.candidate_id for item in self.weights) != tuple(
            sorted(item.candidate_id for item in self.weights)
        ):
            raise HistoricalShadowConfigurationError(
                "Ledger weights must be sorted by candidate ID."
            )
        if len({item.candidate_id for item in self.weights}) != len(self.weights):
            raise HistoricalShadowConfigurationError(
                "Ledger weights cannot repeat candidates."
            )
        _require_unit_decimal(self.cash_weight, "Cash weight")
        _require_nonnegative_decimal(self.one_way_turnover, "One-way turnover")
        total_weight = sum((item.weight for item in self.weights), self.cash_weight)
        if abs(total_weight - _ONE) > Decimal("0.000000000001"):
            raise HistoricalShadowConfigurationError(
                "Ledger weights and cash must sum to one."
            )
        if not isinstance(self.risk_halted, bool):
            raise HistoricalShadowConfigurationError(
                "Risk-halted state must be boolean."
            )

    def to_document(self) -> dict[str, object]:
        return {
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
        }


@dataclass(frozen=True, slots=True)
class ShadowMetrics:
    observation_count: int
    initial_equity: Decimal
    final_equity: Decimal
    cumulative_return: Decimal
    annualized_return: float | None
    annualized_volatility: float
    sharpe_ratio: float | None
    sortino_ratio: float | None
    maximum_drawdown: Decimal
    expected_shortfall: Decimal
    total_turnover: Decimal
    total_cost: Decimal
    average_cash_weight: Decimal
    average_effective_strategy_count: float
    risk_halt_count: int

    def __post_init__(self) -> None:
        _require_positive_int(self.observation_count, "Observation count")
        _require_positive_decimal(self.initial_equity, "Initial equity")
        _require_positive_decimal(self.final_equity, "Final equity")
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
            raise HistoricalShadowConfigurationError(
                "Maximum drawdown cannot be positive."
            )
        _require_finite_decimal(self.expected_shortfall, "Expected shortfall")
        _require_nonnegative_decimal(self.total_turnover, "Total turnover")
        _require_nonnegative_decimal(self.total_cost, "Total cost")
        _require_unit_decimal(self.average_cash_weight, "Average cash weight")
        _require_nonnegative_float(
            self.average_effective_strategy_count,
            "Average effective strategy count",
        )
        _require_nonnegative_int(self.risk_halt_count, "Risk halt count")

    def to_document(self) -> dict[str, object]:
        return {
            "observation_count": self.observation_count,
            "initial_equity": _decimal_text(self.initial_equity),
            "final_equity": _decimal_text(self.final_equity),
            "cumulative_return": _decimal_text(self.cumulative_return),
            "annualized_return": self.annualized_return,
            "annualized_volatility": self.annualized_volatility,
            "sharpe_ratio": self.sharpe_ratio,
            "sortino_ratio": self.sortino_ratio,
            "maximum_drawdown": _decimal_text(self.maximum_drawdown),
            "expected_shortfall": _decimal_text(self.expected_shortfall),
            "total_turnover": _decimal_text(self.total_turnover),
            "total_cost": _decimal_text(self.total_cost),
            "average_cash_weight": _decimal_text(self.average_cash_weight),
            "average_effective_strategy_count": (
                self.average_effective_strategy_count
            ),
            "risk_halt_count": self.risk_halt_count,
        }


@dataclass(frozen=True, slots=True)
class ShadowScenarioResult:
    scenario: ShadowScenarioName
    metrics: ShadowMetrics
    removed_candidate_id: str | None
    passed: bool
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.scenario, ShadowScenarioName):
            raise HistoricalShadowConfigurationError(
                "Shadow scenario must be a ShadowScenarioName."
            )
        if not isinstance(self.metrics, ShadowMetrics):
            raise HistoricalShadowConfigurationError(
                "Shadow scenario metrics must be ShadowMetrics."
            )
        if self.removed_candidate_id is not None:
            _require_nonblank(
                self.removed_candidate_id,
                "Removed candidate ID",
            )
        if not isinstance(self.passed, bool):
            raise HistoricalShadowConfigurationError(
                "Scenario pass state must be boolean."
            )
        if any(not reason.strip() for reason in self.reasons):
            raise HistoricalShadowConfigurationError(
                "Scenario reasons must be nonblank."
            )
        if self.passed != (not self.reasons):
            raise HistoricalShadowConfigurationError(
                "Scenario pass state must agree with reasons."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "scenario": self.scenario.value,
            "metrics": self.metrics.to_document(),
            "removed_candidate_id": self.removed_candidate_id,
            "passed": self.passed,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class HistoricalShadowReport:
    dataset_digest: str
    matrix_digest: str
    manifest_digest: str
    policy: HistoricalShadowPolicy
    journal: tuple[ShadowJournalEntry, ...]
    ledger: tuple[ShadowLedgerSnapshot, ...]
    scenarios: tuple[ShadowScenarioResult, ...]
    decision: ShadowRunDecision
    decision_reasons: tuple[str, ...]
    created_at: datetime
    report_id: str = field(init=False)
    report_digest: str = field(init=False)

    def __post_init__(self) -> None:
        for digest_value, field_name in (
            (self.dataset_digest, "Dataset digest"),
            (self.matrix_digest, "Matrix digest"),
            (self.manifest_digest, "Manifest digest"),
        ):
            _require_sha256(digest_value, field_name)
        if not isinstance(self.policy, HistoricalShadowPolicy):
            raise HistoricalShadowConfigurationError(
                "Historical shadow report policy is invalid."
            )
        if not self.ledger:
            raise HistoricalShadowEligibilityError(
                "Historical shadow report requires ledger observations."
            )
        if tuple(item.sequence for item in self.journal) != tuple(
            range(len(self.journal))
        ):
            raise HistoricalShadowConfigurationError(
                "Journal sequence must be contiguous from zero."
            )
        if tuple(item.timestamp for item in self.ledger) != tuple(
            sorted(item.timestamp for item in self.ledger)
        ):
            raise HistoricalShadowConfigurationError(
                "Ledger timestamps must be sorted."
            )
        expected_scenarios = tuple(ShadowScenarioName)
        if tuple(item.scenario for item in self.scenarios) != expected_scenarios:
            raise HistoricalShadowConfigurationError(
                "Historical shadow scenarios are incomplete or out of order."
            )
        if not isinstance(self.decision, ShadowRunDecision):
            raise HistoricalShadowConfigurationError(
                "Shadow run decision must be a ShadowRunDecision."
            )
        if any(not reason.strip() for reason in self.decision_reasons):
            raise HistoricalShadowConfigurationError(
                "Decision reasons must be nonblank."
            )
        if (
            self.decision
            is ShadowRunDecision.READY_FOR_FORWARD_SHADOW_RESEARCH
            and self.decision_reasons
        ):
            raise HistoricalShadowConfigurationError(
                "Ready reports cannot contain rejection reasons."
            )
        if (
            self.decision
            is not ShadowRunDecision.READY_FOR_FORWARD_SHADOW_RESEARCH
            and not self.decision_reasons
        ):
            raise HistoricalShadowConfigurationError(
                "Non-ready reports require decision reasons."
            )
        _require_aware(self.created_at, "Report creation time")
        identity = self.identity_document()
        report_digest = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
        object.__setattr__(self, "report_digest", report_digest)
        object.__setattr__(
            self,
            "report_id",
            str(uuid5(_REPORT_NAMESPACE, report_digest)),
        )

    @property
    def base_scenario(self) -> ShadowScenarioResult:
        return self.scenarios[0]

    def identity_document(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "execution_mode": "historical_shadow",
            "network_access": "disabled",
            "broker_provider": "none",
            "live_trading": "disabled",
            "order_submission": "disabled",
            "dataset_digest": self.dataset_digest,
            "matrix_digest": self.matrix_digest,
            "manifest_digest": self.manifest_digest,
            "policy": self.policy.to_document(),
            "journal": [item.to_document() for item in self.journal],
            "ledger": [item.to_document() for item in self.ledger],
            "scenarios": [item.to_document() for item in self.scenarios],
            "decision": self.decision.value,
            "decision_reasons": list(self.decision_reasons),
        }

    def to_document(self) -> dict[str, object]:
        document = self.identity_document()
        document["created_at"] = format_utc(self.created_at)
        document["report_id"] = self.report_id
        document["report_digest"] = self.report_digest
        return document


def parse_utc_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise HistoricalShadowConfigurationError(
            f"{field_name} must be a nonblank ISO-8601 string."
        )
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise HistoricalShadowConfigurationError(
            f"{field_name} is not a valid ISO-8601 timestamp."
        ) from error
    _require_aware(parsed, field_name)
    return parsed.astimezone(UTC)


def _decimal_text(value: Decimal) -> str:
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _require_nonblank(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise HistoricalShadowConfigurationError(
            f"{field_name} must be a nonblank string."
        )


def _require_sha256(value: str, field_name: str) -> None:
    _require_nonblank(value, field_name)
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise HistoricalShadowConfigurationError(
            f"{field_name} must be a lowercase SHA-256 digest."
        )


def _require_aware(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise HistoricalShadowConfigurationError(
            f"{field_name} must be timezone-aware."
        )


def _require_positive_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HistoricalShadowConfigurationError(
            f"{field_name} must be a positive integer."
        )


def _require_nonnegative_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HistoricalShadowConfigurationError(
            f"{field_name} must be a nonnegative integer."
        )


def _require_finite_decimal(value: Decimal, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise HistoricalShadowConfigurationError(
            f"{field_name} must be a finite Decimal."
        )


def _require_positive_decimal(value: Decimal, field_name: str) -> None:
    _require_finite_decimal(value, field_name)
    if value <= _ZERO:
        raise HistoricalShadowConfigurationError(
            f"{field_name} must be positive."
        )


def _require_nonnegative_decimal(value: Decimal, field_name: str) -> None:
    _require_finite_decimal(value, field_name)
    if value < _ZERO:
        raise HistoricalShadowConfigurationError(
            f"{field_name} must be nonnegative."
        )


def _require_unit_decimal(value: Decimal, field_name: str) -> None:
    _require_finite_decimal(value, field_name)
    if not (_ZERO <= value <= _ONE):
        raise HistoricalShadowConfigurationError(
            f"{field_name} must be between zero and one."
        )


def _require_open_closed_unit_decimal(value: Decimal, field_name: str) -> None:
    _require_finite_decimal(value, field_name)
    if not (_ZERO < value <= _ONE):
        raise HistoricalShadowConfigurationError(
            f"{field_name} must be greater than zero and at most one."
        )


def _require_optional_finite_float(
    value: float | None,
    field_name: str,
) -> None:
    if value is not None and (not isinstance(value, float) or not math.isfinite(value)):
        raise HistoricalShadowConfigurationError(
            f"{field_name} must be a finite float when provided."
        )


def _require_nonnegative_float(value: float, field_name: str) -> None:
    if not isinstance(value, float) or not math.isfinite(value) or value < 0:
        raise HistoricalShadowConfigurationError(
            f"{field_name} must be a nonnegative finite float."
        )
