from __future__ import annotations

import contextlib
import json
import os
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

from world_quant_system.research.forward_shadow_models import (
    ForwardShadowBatchReport,
    ForwardShadowConfigurationError,
    ForwardShadowIntegrityError,
    ForwardShadowJournalEntry,
    ForwardShadowJournalEventType,
    ForwardShadowLedgerSnapshot,
    ForwardShadowObservation,
    ForwardShadowPolicy,
    ForwardShadowReturn,
    ForwardShadowState,
    PendingForwardAllocation,
)
from world_quant_system.research.historical_shadow_models import (
    ShadowWeight,
    parse_utc_datetime,
)
from world_quant_system.research.portfolio_promotion_models import (
    AllocationMethod,
)


class AtomicJsonForwardShadowStateStore:
    """Load and atomically replace an append-only forward-shadow checkpoint."""

    def __init__(self, state_path: Path) -> None:
        if not isinstance(state_path, Path):
            raise ForwardShadowConfigurationError(
                "Forward-shadow state path must be a pathlib.Path."
            )
        self._state_path = state_path

    def load(self) -> ForwardShadowState | None:
        if not self._state_path.exists():
            return None
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except OSError as error:
            raise ForwardShadowConfigurationError(
                f"Unable to read forward-shadow state: {self._state_path}"
            ) from error
        except json.JSONDecodeError as error:
            raise ForwardShadowIntegrityError(
                "Forward-shadow state file is not valid JSON."
            ) from error
        state = parse_forward_shadow_state(raw)
        stored_digest = raw.get("state_digest") if isinstance(raw, dict) else None
        if stored_digest != state.state_digest:
            raise ForwardShadowIntegrityError(
                "Forward-shadow state digest does not match its contents."
            )
        return state

    def save(self, state: ForwardShadowState) -> None:
        if not isinstance(state, ForwardShadowState):
            raise ForwardShadowConfigurationError(
                "Forward-shadow state store requires a ForwardShadowState."
            )
        _atomic_json_write(self._state_path, state.to_document())


class AtomicJsonForwardShadowReportWriter:
    """Persist a forward-shadow batch report atomically and durably."""

    def __init__(self, output_path: Path) -> None:
        if not isinstance(output_path, Path):
            raise ForwardShadowConfigurationError(
                "Forward-shadow report path must be a pathlib.Path."
            )
        self._output_path = output_path

    def write(self, report: ForwardShadowBatchReport) -> None:
        if not isinstance(report, ForwardShadowBatchReport):
            raise ForwardShadowConfigurationError(
                "Forward-shadow writer requires a batch report."
            )
        _atomic_json_write(self._output_path, report.to_document())


def parse_forward_shadow_state(raw: object) -> ForwardShadowState:
    if not isinstance(raw, dict):
        raise ForwardShadowIntegrityError(
            "Forward-shadow state root must be a JSON object."
        )
    if raw.get("schema_version") != 1:
        raise ForwardShadowIntegrityError(
            "Forward-shadow state schema_version must equal 1."
        )
    if raw.get("execution_mode") != "forward_shadow":
        raise ForwardShadowIntegrityError(
            "Forward-shadow execution mode is invalid."
        )
    for key, expected in (
        ("network_access", "disabled"),
        ("broker_provider", "none"),
        ("live_trading", "disabled"),
        ("order_submission", "disabled"),
    ):
        if raw.get(key) != expected:
            raise ForwardShadowIntegrityError(
                f"Forward-shadow safety field {key} is invalid."
            )
    policy = _parse_policy(_required_mapping(raw, "policy"))
    observations = tuple(
        _parse_observation(item)
        for item in _required_list(raw, "observations")
    )
    journal = tuple(
        _parse_journal_entry(item)
        for item in _required_list(raw, "journal")
    )
    ledger = tuple(
        _parse_ledger_snapshot(item)
        for item in _required_list(raw, "ledger")
    )
    pending_raw = raw.get("pending_allocation")
    pending = None if pending_raw is None else _parse_pending(pending_raw)
    updated_raw = raw.get("updated_at")
    updated_at = (
        None
        if updated_raw is None
        else parse_utc_datetime(updated_raw, "updated_at")
    )
    visible_raw = _required_list(raw, "visible_event_ids")
    visible_event_ids: list[tuple[str, str]] = []
    for item in visible_raw:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(value, str) for value in item)
        ):
            raise ForwardShadowIntegrityError(
                "Visible event IDs must contain string pairs."
            )
        visible_event_ids.append((item[0], item[1]))
    return ForwardShadowState(
        dataset_digest=_required_text(raw, "dataset_digest"),
        manifest_digest=_required_text(raw, "manifest_digest"),
        manifest_event_ids=tuple(
            _required_string_list(raw, "manifest_event_ids")
        ),
        policy=policy,
        observations=observations,
        journal=journal,
        ledger=ledger,
        active_weights=_parse_weights(_required_list(raw, "active_weights")),
        cash_weight=_required_decimal(raw, "cash_weight"),
        pending_allocation=pending,
        equity=_required_decimal(raw, "equity"),
        peak_equity=_required_decimal(raw, "peak_equity"),
        risk_halted=_required_bool(raw, "risk_halted"),
        feed_stale=_required_bool(raw, "feed_stale"),
        visible_event_ids=tuple(visible_event_ids),
        updated_at=updated_at,
    )


def _parse_policy(raw: dict[str, object]) -> ForwardShadowPolicy:
    try:
        allocation_method = AllocationMethod(
            _required_text(raw, "allocation_method")
        )
    except ValueError as error:
        raise ForwardShadowIntegrityError(
            "Stored allocation method is unsupported."
        ) from error
    return ForwardShadowPolicy(
        initial_equity=_required_decimal(raw, "initial_equity"),
        allocation_method=allocation_method,
        rebalance_every_observations=_required_int(
            raw,
            "rebalance_every_observations",
        ),
        volatility_lookback=_required_int(raw, "volatility_lookback"),
        execution_delay_observations=_required_int(
            raw,
            "execution_delay_observations",
        ),
        minimum_active_candidates=_required_int(
            raw,
            "minimum_active_candidates",
        ),
        minimum_cash_weight=_required_decimal(raw, "minimum_cash_weight"),
        maximum_candidate_weight=_required_decimal(
            raw,
            "maximum_candidate_weight",
        ),
        maximum_one_way_turnover=_required_decimal(
            raw,
            "maximum_one_way_turnover",
        ),
        transaction_cost_bps=_required_decimal(raw, "transaction_cost_bps"),
        maximum_evidence_age_days=_required_int(
            raw,
            "maximum_evidence_age_days",
        ),
        maximum_observation_lateness_seconds=_required_int(
            raw,
            "maximum_observation_lateness_seconds",
        ),
        maximum_future_clock_skew_seconds=_required_int(
            raw,
            "maximum_future_clock_skew_seconds",
        ),
        maximum_drawdown_before_halt=_required_decimal(
            raw,
            "maximum_drawdown_before_halt",
        ),
    )


def _parse_observation(raw: object) -> ForwardShadowObservation:
    mapping = _as_mapping(raw, "Stored observation")
    returns = tuple(
        ForwardShadowReturn(
            candidate_id=_required_text(
                _as_mapping(item, "Stored return"),
                "candidate_id",
            ),
            value=_required_decimal(
                _as_mapping(item, "Stored return"),
                "value",
            ),
        )
        for item in _required_list(mapping, "returns")
    )
    observation = ForwardShadowObservation(
        sequence=_required_int(mapping, "sequence"),
        observed_at=parse_utc_datetime(
            mapping.get("observed_at"),
            "observed_at",
        ),
        received_at=parse_utc_datetime(
            mapping.get("received_at"),
            "received_at",
        ),
        returns=returns,
        source_digest=_required_text(mapping, "source_digest"),
    )
    if mapping.get("observation_id") != observation.observation_id:
        raise ForwardShadowIntegrityError(
            "Stored observation ID does not match its contents."
        )
    return observation


def _parse_journal_entry(raw: object) -> ForwardShadowJournalEntry:
    mapping = _as_mapping(raw, "Stored journal entry")
    try:
        event_type = ForwardShadowJournalEventType(
            _required_text(mapping, "event_type")
        )
    except ValueError as error:
        raise ForwardShadowIntegrityError(
            "Stored journal event type is unsupported."
        ) from error
    observation_id = mapping.get("observation_id")
    candidate_id = mapping.get("candidate_id")
    if observation_id is not None and not isinstance(observation_id, str):
        raise ForwardShadowIntegrityError(
            "Stored journal observation ID must be a string or null."
        )
    if candidate_id is not None and not isinstance(candidate_id, str):
        raise ForwardShadowIntegrityError(
            "Stored journal candidate ID must be a string or null."
        )
    entry = ForwardShadowJournalEntry(
        sequence=_required_int(mapping, "sequence"),
        timestamp=parse_utc_datetime(mapping.get("timestamp"), "timestamp"),
        event_type=event_type,
        reason=_required_text(mapping, "reason"),
        observation_id=observation_id,
        candidate_id=candidate_id,
        previous_hash=_required_text(mapping, "previous_hash"),
    )
    if mapping.get("entry_hash") != entry.entry_hash:
        raise ForwardShadowIntegrityError(
            "Stored journal hash does not match its contents."
        )
    return entry


def _parse_ledger_snapshot(raw: object) -> ForwardShadowLedgerSnapshot:
    mapping = _as_mapping(raw, "Stored ledger snapshot")
    return ForwardShadowLedgerSnapshot(
        observation_id=_required_text(mapping, "observation_id"),
        sequence=_required_int(mapping, "sequence"),
        timestamp=parse_utc_datetime(mapping.get("timestamp"), "timestamp"),
        equity_before=_required_decimal(mapping, "equity_before"),
        execution_cost=_required_decimal(mapping, "execution_cost"),
        gross_period_return=_required_decimal(mapping, "gross_period_return"),
        net_period_return=_required_decimal(mapping, "net_period_return"),
        equity_after=_required_decimal(mapping, "equity_after"),
        drawdown=_required_decimal(mapping, "drawdown"),
        weights=_parse_weights(_required_list(mapping, "weights")),
        cash_weight=_required_decimal(mapping, "cash_weight"),
        one_way_turnover=_required_decimal(mapping, "one_way_turnover"),
        risk_halted=_required_bool(mapping, "risk_halted"),
        feed_stale=_required_bool(mapping, "feed_stale"),
    )


def _parse_pending(raw: object) -> PendingForwardAllocation:
    mapping = _as_mapping(raw, "Stored pending allocation")
    return PendingForwardAllocation(
        execute_sequence=_required_int(mapping, "execute_sequence"),
        weights=_parse_weights(_required_list(mapping, "weights")),
        cash_weight=_required_decimal(mapping, "cash_weight"),
        one_way_turnover=_required_decimal(mapping, "one_way_turnover"),
        reason=_required_text(mapping, "reason"),
    )


def _parse_weights(raw: list[object]) -> tuple[ShadowWeight, ...]:
    weights: list[ShadowWeight] = []
    for item in raw:
        mapping = _as_mapping(item, "Stored weight")
        weights.append(
            ShadowWeight(
                candidate_id=_required_text(mapping, "candidate_id"),
                weight=_required_decimal(mapping, "weight"),
            )
        )
    return tuple(weights)


def _atomic_json_write(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                document,
                stream,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
        raise


def _as_mapping(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ForwardShadowIntegrityError(
            f"{field_name} must be a JSON object."
        )
    return value


def _required_mapping(
    raw: dict[str, object],
    key: str,
) -> dict[str, object]:
    return _as_mapping(raw.get(key), key)


def _required_list(raw: dict[str, object], key: str) -> list[object]:
    value = raw.get(key)
    if not isinstance(value, list):
        raise ForwardShadowIntegrityError(f"{key} must be a JSON array.")
    return value


def _required_string_list(
    raw: dict[str, object],
    key: str,
) -> list[str]:
    values = _required_list(raw, key)
    if not all(isinstance(value, str) for value in values):
        raise ForwardShadowIntegrityError(
            f"{key} must contain only strings."
        )
    return [value for value in values if isinstance(value, str)]


def _required_text(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ForwardShadowIntegrityError(
            f"{key} must be a nonblank string."
        )
    return value


def _required_decimal(raw: dict[str, object], key: str) -> Decimal:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ForwardShadowIntegrityError(
            f"{key} must be a decimal-compatible value."
        )
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as error:
        raise ForwardShadowIntegrityError(
            f"{key} is not a valid decimal."
        ) from error
    if not parsed.is_finite():
        raise ForwardShadowIntegrityError(f"{key} must be finite.")
    return parsed


def _required_int(raw: dict[str, object], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ForwardShadowIntegrityError(f"{key} must be an integer.")
    return value


def _required_bool(raw: dict[str, object], key: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise ForwardShadowIntegrityError(f"{key} must be boolean.")
    return value
