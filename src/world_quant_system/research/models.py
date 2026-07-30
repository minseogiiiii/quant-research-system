from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast
from uuid import UUID, uuid5

from world_quant_system.data.normalized_models import canonical_json_bytes, format_utc

_SCHEMA_VERSION = 1
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")
_SEMVER_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_EXPERIMENT_NAMESPACE = UUID("2a37ce7c-34ec-5caf-9ca8-c818be5ebdf2")


class ResearchError(Exception):
    """Base exception for research-validity failures."""


class ResearchConfigurationError(ResearchError):
    """Raised when a research configuration is invalid."""


class ResearchConflictError(ResearchError):
    """Raised when an immutable research record conflicts with stored data."""


class ResearchIntegrityError(ResearchError):
    """Raised when stored research data fails integrity validation."""


class ResearchNotFoundError(ResearchError):
    """Raised when a research experiment does not exist."""


class HoldoutConsumedError(ResearchError):
    """Raised when an untouched holdout is evaluated more than once."""


class ExperimentStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ResearchWindow:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        _require_aware(self.start, "Research-window start")
        _require_aware(self.end, "Research-window end")
        if self.start >= self.end:
            raise ResearchConfigurationError(
                "Research windows must use a non-empty half-open interval [start, end)."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "start": format_utc(self.start),
            "end": format_utc(self.end),
        }


@dataclass(frozen=True, slots=True)
class ResearchSplit:
    train: ResearchWindow
    validation: ResearchWindow
    holdout: ResearchWindow

    def __post_init__(self) -> None:
        if not all(
            isinstance(window, ResearchWindow)
            for window in (self.train, self.validation, self.holdout)
        ):
            raise ResearchConfigurationError(
                "Research split windows must be ResearchWindow values."
            )
        if self.train.end > self.validation.start:
            raise ResearchConfigurationError(
                "Train and validation windows cannot overlap."
            )
        if self.validation.end > self.holdout.start:
            raise ResearchConfigurationError(
                "Validation and holdout windows cannot overlap."
            )

    @property
    def fingerprint(self) -> str:
        return _sha256(self.to_document())

    def to_document(self) -> dict[str, object]:
        return {
            "train": self.train.to_document(),
            "validation": self.validation.to_document(),
            "holdout": self.holdout.to_document(),
        }


@dataclass(frozen=True, slots=True)
class ParameterSearchAudit:
    search_id: str
    search_space_json: str
    trial_number: int
    total_trials: int
    selection_metric: str

    def __post_init__(self) -> None:
        _require_nonblank(self.search_id, "Search ID")
        _validate_canonical_json_object(
            self.search_space_json,
            "Search space JSON",
        )
        _require_positive_int(self.trial_number, "Trial number")
        _require_positive_int(self.total_trials, "Total trials")
        if self.trial_number > self.total_trials:
            raise ResearchConfigurationError(
                "Trial number cannot exceed total trials."
            )
        _require_nonblank(self.selection_metric, "Selection metric")

    def to_document(self) -> dict[str, object]:
        return {
            "search_id": self.search_id,
            "search_space": _json_object(self.search_space_json),
            "trial_number": self.trial_number,
            "total_trials": self.total_trials,
            "selection_metric": self.selection_metric,
        }


@dataclass(frozen=True, slots=True)
class ExperimentSpec:
    strategy_name: str
    strategy_version: str
    parameters_json: str
    dataset_digest: str
    code_commit: str
    cost_model_json: str
    execution_model_json: str
    split: ResearchSplit
    search_audit: ParameterSearchAudit
    parent_experiment_id: str | None = None
    change_reason: str | None = None

    def __post_init__(self) -> None:
        _require_nonblank(self.strategy_name, "Strategy name")
        if not isinstance(self.strategy_version, str) or not _SEMVER_PATTERN.fullmatch(
            self.strategy_version
        ):
            raise ResearchConfigurationError(
                "Strategy version must use semantic x.y.z form."
            )
        _validate_canonical_json_object(self.parameters_json, "Parameters JSON")
        _validate_sha256(self.dataset_digest, "Dataset digest")
        if not isinstance(self.code_commit, str) or not _GIT_COMMIT_PATTERN.fullmatch(
            self.code_commit
        ):
            raise ResearchConfigurationError(
                "Code commit must be a lowercase hexadecimal Git object ID."
            )
        _validate_canonical_json_object(self.cost_model_json, "Cost model JSON")
        _validate_canonical_json_object(
            self.execution_model_json,
            "Execution model JSON",
        )
        if not isinstance(self.split, ResearchSplit):
            raise ResearchConfigurationError(
                "Experiment split must be a ResearchSplit."
            )
        if not isinstance(self.search_audit, ParameterSearchAudit):
            raise ResearchConfigurationError(
                "Experiment search audit must be a ParameterSearchAudit."
            )
        if (
            self.parent_experiment_id is None
            and self.change_reason is not None
        ):
            raise ResearchConfigurationError(
                "Change reason requires a parent experiment."
            )
        if self.parent_experiment_id is not None:
            _validate_uuid(self.parent_experiment_id, "Parent experiment ID")
            _require_nonblank(self.change_reason, "Change reason")

    @property
    def research_digest(self) -> str:
        return _sha256(self.to_document())

    @property
    def experiment_id(self) -> str:
        return str(uuid5(_EXPERIMENT_NAMESPACE, self.research_digest))

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "strategy": {
                "name": self.strategy_name,
                "version": self.strategy_version,
                "parameters": _json_object(self.parameters_json),
            },
            "dataset_digest": self.dataset_digest,
            "code_commit": self.code_commit,
            "cost_model": _json_object(self.cost_model_json),
            "execution_model": _json_object(self.execution_model_json),
            "split": self.split.to_document(),
            "search_audit": self.search_audit.to_document(),
            "parent_experiment_id": self.parent_experiment_id,
            "change_reason": self.change_reason,
        }


@dataclass(frozen=True, slots=True)
class ExperimentRecord:
    experiment_id: str
    research_digest: str
    spec: ExperimentSpec
    registered_at: datetime

    def __post_init__(self) -> None:
        _validate_uuid(self.experiment_id, "Experiment ID")
        _validate_sha256(self.research_digest, "Research digest")
        if not isinstance(self.spec, ExperimentSpec):
            raise ResearchConfigurationError(
                "Experiment record must contain an ExperimentSpec."
            )
        _require_aware(self.registered_at, "Registered-at timestamp")
        if self.research_digest != self.spec.research_digest:
            raise ResearchIntegrityError(
                "Experiment record research digest does not match its specification."
            )
        if self.experiment_id != self.spec.experiment_id:
            raise ResearchIntegrityError(
                "Experiment ID does not match the deterministic research digest."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "research_digest": self.research_digest,
            "registered_at": format_utc(self.registered_at),
            "spec": self.spec.to_document(),
        }


@dataclass(frozen=True, slots=True)
class ExperimentOutcome:
    experiment_id: str
    status: ExperimentStatus
    completed_at: datetime
    result_digest: str | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        _validate_uuid(self.experiment_id, "Experiment ID")
        if not isinstance(self.status, ExperimentStatus):
            raise ResearchConfigurationError(
                "Experiment outcome status must be an ExperimentStatus value."
            )
        _require_aware(self.completed_at, "Completed-at timestamp")
        if self.status is ExperimentStatus.SUCCEEDED:
            _validate_sha256(self.result_digest, "Result digest")
            if self.failure_reason is not None:
                raise ResearchConfigurationError(
                    "Successful experiments cannot have a failure reason."
                )
        else:
            if self.result_digest is not None:
                raise ResearchConfigurationError(
                    "Failed experiments cannot have a result digest."
                )
            _require_nonblank(self.failure_reason, "Failure reason")

    def to_document(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "status": self.status.value,
            "completed_at": format_utc(self.completed_at),
            "result_digest": self.result_digest,
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True, slots=True)
class HoldoutConsumption:
    experiment_id: str
    result_digest: str
    consumed_at: datetime

    def __post_init__(self) -> None:
        _validate_uuid(self.experiment_id, "Experiment ID")
        _validate_sha256(self.result_digest, "Holdout result digest")
        _require_aware(self.consumed_at, "Holdout-consumed timestamp")

    def to_document(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "result_digest": self.result_digest,
            "consumed_at": format_utc(self.consumed_at),
        }


@dataclass(frozen=True, slots=True)
class ExperimentSnapshot:
    record: ExperimentRecord
    outcome: ExperimentOutcome | None
    holdout_consumption: HoldoutConsumption | None

    def __post_init__(self) -> None:
        if not isinstance(self.record, ExperimentRecord):
            raise ResearchConfigurationError(
                "Experiment snapshot must contain an ExperimentRecord."
            )
        if (
            self.outcome is not None
            and self.outcome.experiment_id != self.record.experiment_id
        ):
            raise ResearchIntegrityError(
                "Experiment outcome belongs to a different experiment."
            )
        if (
            self.holdout_consumption is not None
            and self.holdout_consumption.experiment_id != self.record.experiment_id
        ):
            raise ResearchIntegrityError(
                "Holdout consumption belongs to a different experiment."
            )


def canonical_json_object(value: Mapping[str, object]) -> str:
    """Return a deterministic JSON object string for research fingerprints."""

    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ResearchConfigurationError(
            "Research JSON values must be finite JSON-compatible objects."
        ) from error
    parsed = json.loads(encoded)
    if not isinstance(parsed, dict):
        raise ResearchConfigurationError("Research JSON must be an object.")
    return encoded


def experiment_spec_from_document(document: Mapping[str, object]) -> ExperimentSpec:
    if _required_int(document, "schema_version") != _SCHEMA_VERSION:
        raise ResearchIntegrityError("Stored research schema version is unsupported.")
    strategy = _required_mapping(document, "strategy")
    split_document = _required_mapping(document, "split")
    audit_document = _required_mapping(document, "search_audit")
    parent = document.get("parent_experiment_id")
    change_reason = document.get("change_reason")
    if parent is not None and not isinstance(parent, str):
        raise ResearchIntegrityError("Stored parent experiment ID is invalid.")
    if change_reason is not None and not isinstance(change_reason, str):
        raise ResearchIntegrityError("Stored change reason is invalid.")
    return ExperimentSpec(
        strategy_name=_required_string(strategy, "name"),
        strategy_version=_required_string(strategy, "version"),
        parameters_json=canonical_json_object(
            _required_mapping(strategy, "parameters")
        ),
        dataset_digest=_required_string(document, "dataset_digest"),
        code_commit=_required_string(document, "code_commit"),
        cost_model_json=canonical_json_object(
            _required_mapping(document, "cost_model")
        ),
        execution_model_json=canonical_json_object(
            _required_mapping(document, "execution_model")
        ),
        split=ResearchSplit(
            train=_window_from_document(_required_mapping(split_document, "train")),
            validation=_window_from_document(
                _required_mapping(split_document, "validation")
            ),
            holdout=_window_from_document(
                _required_mapping(split_document, "holdout")
            ),
        ),
        search_audit=ParameterSearchAudit(
            search_id=_required_string(audit_document, "search_id"),
            search_space_json=canonical_json_object(
                _required_mapping(audit_document, "search_space")
            ),
            trial_number=_required_int(audit_document, "trial_number"),
            total_trials=_required_int(audit_document, "total_trials"),
            selection_metric=_required_string(audit_document, "selection_metric"),
        ),
        parent_experiment_id=parent,
        change_reason=change_reason,
    )


def experiment_record_from_document(
    document: Mapping[str, object],
) -> ExperimentRecord:
    return ExperimentRecord(
        experiment_id=_required_string(document, "experiment_id"),
        research_digest=_required_string(document, "research_digest"),
        registered_at=_parse_utc(_required_string(document, "registered_at")),
        spec=experiment_spec_from_document(_required_mapping(document, "spec")),
    )


def experiment_outcome_from_document(
    document: Mapping[str, object],
) -> ExperimentOutcome:
    result_digest = document.get("result_digest")
    failure_reason = document.get("failure_reason")
    if result_digest is not None and not isinstance(result_digest, str):
        raise ResearchIntegrityError("Stored result digest is invalid.")
    if failure_reason is not None and not isinstance(failure_reason, str):
        raise ResearchIntegrityError("Stored failure reason is invalid.")
    try:
        status = ExperimentStatus(_required_string(document, "status"))
    except ValueError as error:
        raise ResearchIntegrityError("Stored experiment status is invalid.") from error
    return ExperimentOutcome(
        experiment_id=_required_string(document, "experiment_id"),
        status=status,
        completed_at=_parse_utc(_required_string(document, "completed_at")),
        result_digest=result_digest,
        failure_reason=failure_reason,
    )


def holdout_consumption_from_document(
    document: Mapping[str, object],
) -> HoldoutConsumption:
    return HoldoutConsumption(
        experiment_id=_required_string(document, "experiment_id"),
        result_digest=_required_string(document, "result_digest"),
        consumed_at=_parse_utc(_required_string(document, "consumed_at")),
    )


def _window_from_document(document: Mapping[str, object]) -> ResearchWindow:
    return ResearchWindow(
        start=_parse_utc(_required_string(document, "start")),
        end=_parse_utc(_required_string(document, "end")),
    )


def _sha256(document: object) -> str:
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


def _validate_canonical_json_object(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise ResearchConfigurationError(f"{field_name} must be a JSON string.")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ResearchConfigurationError(f"{field_name} must be valid JSON.") from error
    if not isinstance(parsed, dict):
        raise ResearchConfigurationError(f"{field_name} must contain a JSON object.")
    if canonical_json_object(cast(Mapping[str, object], parsed)) != value:
        raise ResearchConfigurationError(
            f"{field_name} must use canonical sorted JSON form."
        )


def _json_object(value: str) -> dict[str, object]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ResearchIntegrityError("Canonical research JSON is not an object.")
    return cast(dict[str, object], parsed)


def _validate_sha256(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise ResearchConfigurationError(
            f"{field_name} must be a lowercase SHA-256 digest."
        )


def _validate_uuid(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise ResearchConfigurationError(f"{field_name} must be a UUID string.")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ResearchConfigurationError(
            f"{field_name} must be a UUID string."
        ) from error
    if str(parsed) != value:
        raise ResearchConfigurationError(
            f"{field_name} must use canonical UUID form."
        )


def _require_aware(value: object, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ResearchConfigurationError(
            f"{field_name} must include timezone information."
        )


def _require_nonblank(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ResearchConfigurationError(f"{field_name} must be a nonblank string.")


def _require_positive_int(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ResearchConfigurationError(f"{field_name} must be a positive integer.")


def _required_mapping(
    document: Mapping[str, object],
    key: str,
) -> Mapping[str, object]:
    value = document.get(key)
    if not isinstance(value, dict):
        raise ResearchIntegrityError(f"Stored research field {key!r} is invalid.")
    return cast(Mapping[str, object], value)


def _required_string(document: Mapping[str, object], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str):
        raise ResearchIntegrityError(f"Stored research field {key!r} is invalid.")
    return value


def _required_int(document: Mapping[str, object], key: str) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResearchIntegrityError(f"Stored research field {key!r} is invalid.")
    return value


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ResearchIntegrityError(
            "Stored research timestamp is not valid ISO-8601."
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResearchIntegrityError(
            "Stored research timestamp lacks timezone information."
        )
    return parsed.astimezone(UTC)
