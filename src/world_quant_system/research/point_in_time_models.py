from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast
from uuid import UUID, uuid5

from world_quant_system.data.normalized_models import canonical_json_bytes, format_utc
from world_quant_system.research.models import (
    ResearchConfigurationError,
    ResearchConflictError,
    ResearchError,
    ResearchIntegrityError,
    ResearchNotFoundError,
)

_SCHEMA_VERSION = 1
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RECORD_NAMESPACE = UUID("28b18cd1-c622-56f5-ac31-7b847014af45")


class PointInTimeError(ResearchError):
    """Base exception for point-in-time data-integrity failures."""


class PointInTimeConfigurationError(PointInTimeError, ResearchConfigurationError):
    """Raised when point-in-time metadata or policy is invalid."""


class PointInTimeConflictError(PointInTimeError, ResearchConflictError):
    """Raised when immutable point-in-time metadata conflicts."""


class PointInTimeIntegrityError(PointInTimeError, ResearchIntegrityError):
    """Raised when stored point-in-time metadata fails integrity checks."""


class PointInTimeNotFoundError(PointInTimeError, ResearchNotFoundError):
    """Raised when required point-in-time metadata does not exist."""


class PointInTimeEligibilityError(PointInTimeError):
    """Raised when historical data was not eligible at the decision time."""


class PointInTimeOverlapError(PointInTimeConflictError):
    """Raised when immutable membership intervals overlap."""


class PointInTimeDataKind(StrEnum):
    CANDLE = "candle"
    QUOTE = "quote"
    FUNDAMENTAL = "fundamental"
    CORPORATE_ACTION = "corporate_action"
    UNIVERSE_MEMBERSHIP = "universe_membership"
    OTHER = "other"


class DelistingReason(StrEnum):
    BANKRUPTCY = "bankruptcy"
    MERGER = "merger"
    REGULATORY = "regulatory"
    VOLUNTARY = "voluntary"
    OTHER = "other"
    UNKNOWN = "unknown"


class AsOfPolicy(StrEnum):
    EFFECTIVE_AND_AVAILABLE = "effective_and_available"


class AvailabilityPolicy(StrEnum):
    REQUIRE_RECORD = "require_record"


class DelistingPolicy(StrEnum):
    REQUIRE_RECORD = "require_record"


@dataclass(frozen=True, slots=True)
class PointInTimePolicy:
    as_of_policy: AsOfPolicy = AsOfPolicy.EFFECTIVE_AND_AVAILABLE
    availability_policy: AvailabilityPolicy = AvailabilityPolicy.REQUIRE_RECORD
    delisting_policy: DelistingPolicy = DelistingPolicy.REQUIRE_RECORD

    def __post_init__(self) -> None:
        if not isinstance(self.as_of_policy, AsOfPolicy):
            raise PointInTimeConfigurationError(
                "As-of policy must be an AsOfPolicy value."
            )
        if not isinstance(self.availability_policy, AvailabilityPolicy):
            raise PointInTimeConfigurationError(
                "Availability policy must be an AvailabilityPolicy value."
            )
        if not isinstance(self.delisting_policy, DelistingPolicy):
            raise PointInTimeConfigurationError(
                "Delisting policy must be a DelistingPolicy value."
            )

    @property
    def fingerprint(self) -> str:
        return _sha256(self.to_document())

    def to_document(self) -> dict[str, object]:
        return {
            "as_of_policy": self.as_of_policy.value,
            "availability_policy": self.availability_policy.value,
            "delisting_policy": self.delisting_policy.value,
        }


@dataclass(frozen=True, slots=True)
class SecurityLifecycle:
    exchange: str
    symbol: str
    listed_at: datetime
    tradable_from: datetime
    source: str
    source_digest: str
    delisted_at: datetime | None = None
    tradable_until: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "exchange",
            _normalized_code(self.exchange, "Exchange"),
        )
        object.__setattr__(self, "symbol", _normalized_code(self.symbol, "Symbol"))
        _require_aware(self.listed_at, "Listed-at timestamp")
        _require_aware(self.tradable_from, "Tradable-from timestamp")
        _require_optional_aware(self.delisted_at, "Delisted-at timestamp")
        _require_optional_aware(self.tradable_until, "Tradable-until timestamp")
        if self.tradable_from < self.listed_at:
            raise PointInTimeConfigurationError(
                "Tradable-from timestamp cannot precede listing."
            )
        if self.delisted_at is not None and self.delisted_at <= self.listed_at:
            raise PointInTimeConfigurationError(
                "Delisted-at timestamp must follow listing."
            )
        if (
            self.tradable_until is not None
            and self.tradable_until <= self.tradable_from
        ):
            raise PointInTimeConfigurationError(
                "Tradable-until timestamp must follow tradable-from."
            )
        if (
            self.delisted_at is not None
            and self.tradable_until is not None
            and self.tradable_until > self.delisted_at
        ):
            raise PointInTimeConfigurationError(
                "Tradable-until timestamp cannot follow delisting."
            )
        _require_nonblank(self.source, "Lifecycle source")
        _validate_sha256(self.source_digest, "Lifecycle source digest")

    @property
    def lifecycle_id(self) -> str:
        return _record_id("security-lifecycle", self.to_document())

    def is_listed_at(self, value: datetime) -> bool:
        _require_aware(value, "Listing evaluation timestamp")
        instant = value.astimezone(UTC)
        return self.listed_at.astimezone(UTC) <= instant and (
            self.delisted_at is None
            or instant < self.delisted_at.astimezone(UTC)
        )

    def is_tradable_at(self, value: datetime) -> bool:
        _require_aware(value, "Tradability evaluation timestamp")
        instant = value.astimezone(UTC)
        return self.tradable_from.astimezone(UTC) <= instant and (
            self.tradable_until is None
            or instant < self.tradable_until.astimezone(UTC)
        )

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "listed_at": format_utc(self.listed_at),
            "tradable_from": format_utc(self.tradable_from),
            "delisted_at": (
                None if self.delisted_at is None else format_utc(self.delisted_at)
            ),
            "tradable_until": (
                None
                if self.tradable_until is None
                else format_utc(self.tradable_until)
            ),
            "source": self.source,
            "source_digest": self.source_digest,
        }


@dataclass(frozen=True, slots=True)
class UniverseMembership:
    universe_id: str
    exchange: str
    symbol: str
    member_from: datetime
    available_at: datetime
    source: str
    source_digest: str
    member_until: datetime | None = None

    def __post_init__(self) -> None:
        _require_nonblank(self.universe_id, "Universe ID")
        object.__setattr__(
            self,
            "exchange",
            _normalized_code(self.exchange, "Exchange"),
        )
        object.__setattr__(self, "symbol", _normalized_code(self.symbol, "Symbol"))
        _require_aware(self.member_from, "Membership start")
        _require_optional_aware(self.member_until, "Membership end")
        _require_aware(self.available_at, "Membership available-at timestamp")
        if self.member_until is not None and self.member_until <= self.member_from:
            raise PointInTimeConfigurationError(
                "Membership end must follow membership start."
            )
        _require_nonblank(self.source, "Membership source")
        _validate_sha256(self.source_digest, "Membership source digest")

    @property
    def membership_id(self) -> str:
        return _record_id("universe-membership", self.to_document())

    def is_effective_at(self, value: datetime) -> bool:
        _require_aware(value, "Membership evaluation timestamp")
        instant = value.astimezone(UTC)
        return self.member_from.astimezone(UTC) <= instant and (
            self.member_until is None
            or instant < self.member_until.astimezone(UTC)
        )

    def is_known_at(self, value: datetime) -> bool:
        _require_aware(value, "Membership knowledge timestamp")
        return self.available_at.astimezone(UTC) <= value.astimezone(UTC)

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "universe_id": self.universe_id,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "member_from": format_utc(self.member_from),
            "member_until": (
                None if self.member_until is None else format_utc(self.member_until)
            ),
            "available_at": format_utc(self.available_at),
            "source": self.source,
            "source_digest": self.source_digest,
        }


@dataclass(frozen=True, slots=True)
class DataAvailabilityRecord:
    data_id: str
    data_kind: PointInTimeDataKind
    exchange: str
    symbol: str
    effective_at: datetime
    available_at: datetime
    source: str
    source_digest: str

    def __post_init__(self) -> None:
        _require_nonblank(self.data_id, "Data ID")
        if not isinstance(self.data_kind, PointInTimeDataKind):
            raise PointInTimeConfigurationError(
                "Data kind must be a PointInTimeDataKind value."
            )
        object.__setattr__(
            self,
            "exchange",
            _normalized_code(self.exchange, "Exchange"),
        )
        object.__setattr__(self, "symbol", _normalized_code(self.symbol, "Symbol"))
        _require_aware(self.effective_at, "Data effective-at timestamp")
        _require_aware(self.available_at, "Data available-at timestamp")
        if (
            self.data_kind
            in (PointInTimeDataKind.CANDLE, PointInTimeDataKind.QUOTE)
            and self.available_at < self.effective_at
        ):
            raise PointInTimeConfigurationError(
                "Market data cannot be available before its effective timestamp."
            )
        _require_nonblank(self.source, "Data source")
        _validate_sha256(self.source_digest, "Data source digest")

    @property
    def availability_id(self) -> str:
        return _record_id("data-availability", self.to_document())

    def is_available_at(self, value: datetime) -> bool:
        _require_aware(value, "Data decision timestamp")
        return self.available_at.astimezone(UTC) <= value.astimezone(UTC)

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "data_id": self.data_id,
            "data_kind": self.data_kind.value,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "effective_at": format_utc(self.effective_at),
            "available_at": format_utc(self.available_at),
            "source": self.source,
            "source_digest": self.source_digest,
        }


@dataclass(frozen=True, slots=True)
class DelistingRecord:
    exchange: str
    symbol: str
    last_tradable_at: datetime
    delisted_at: datetime
    available_at: datetime
    reason: DelistingReason
    source: str
    source_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "exchange",
            _normalized_code(self.exchange, "Exchange"),
        )
        object.__setattr__(self, "symbol", _normalized_code(self.symbol, "Symbol"))
        _require_aware(self.last_tradable_at, "Last-tradable timestamp")
        _require_aware(self.delisted_at, "Delisted-at timestamp")
        _require_aware(self.available_at, "Delisting available-at timestamp")
        if self.last_tradable_at >= self.delisted_at:
            raise PointInTimeConfigurationError(
                "Last-tradable timestamp must precede delisting."
            )
        if not isinstance(self.reason, DelistingReason):
            raise PointInTimeConfigurationError(
                "Delisting reason must be a DelistingReason value."
            )
        _require_nonblank(self.source, "Delisting source")
        _validate_sha256(self.source_digest, "Delisting source digest")

    @property
    def delisting_id(self) -> str:
        return _record_id("delisting", self.to_document())

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "last_tradable_at": format_utc(self.last_tradable_at),
            "delisted_at": format_utc(self.delisted_at),
            "available_at": format_utc(self.available_at),
            "reason": self.reason.value,
            "source": self.source,
            "source_digest": self.source_digest,
        }


@dataclass(frozen=True, slots=True)
class PointInTimeMember:
    exchange: str
    symbol: str
    lifecycle_id: str
    membership_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "exchange",
            _normalized_code(self.exchange, "Exchange"),
        )
        object.__setattr__(self, "symbol", _normalized_code(self.symbol, "Symbol"))
        _validate_uuid(self.lifecycle_id, "Lifecycle ID")
        _validate_uuid(self.membership_id, "Membership ID")

    def to_document(self) -> dict[str, object]:
        return {
            "exchange": self.exchange,
            "symbol": self.symbol,
            "lifecycle_id": self.lifecycle_id,
            "membership_id": self.membership_id,
        }


@dataclass(frozen=True, slots=True)
class PointInTimeSnapshot:
    universe_id: str
    as_of: datetime
    policy: PointInTimePolicy
    members: tuple[PointInTimeMember, ...]

    def __post_init__(self) -> None:
        _require_nonblank(self.universe_id, "Universe ID")
        _require_aware(self.as_of, "Snapshot as-of timestamp")
        if not isinstance(self.policy, PointInTimePolicy):
            raise PointInTimeConfigurationError(
                "Snapshot policy must be a PointInTimePolicy."
            )
        if not isinstance(self.members, tuple) or not self.members:
            raise PointInTimeConfigurationError(
                "Point-in-time snapshots require at least one member."
            )
        if not all(isinstance(member, PointInTimeMember) for member in self.members):
            raise PointInTimeConfigurationError(
                "Snapshot members must be PointInTimeMember values."
            )
        ordered = tuple(
            sorted(self.members, key=lambda item: (item.exchange, item.symbol))
        )
        if ordered != self.members:
            raise PointInTimeConfigurationError(
                "Snapshot members must be deterministically sorted."
            )
        keys = [(member.exchange, member.symbol) for member in self.members]
        if len(keys) != len(set(keys)):
            raise PointInTimeConfigurationError(
                "Snapshot members cannot contain duplicate securities."
            )

    @property
    def universe_digest(self) -> str:
        return _sha256(
            {
                "universe_id": self.universe_id,
                "members": [member.to_document() for member in self.members],
            }
        )

    @property
    def snapshot_digest(self) -> str:
        return _sha256(self.to_document())

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "universe_id": self.universe_id,
            "as_of": format_utc(self.as_of),
            "policy": self.policy.to_document(),
            "universe_digest": self.universe_digest,
            "members": [member.to_document() for member in self.members],
        }


@dataclass(frozen=True, slots=True)
class PointInTimeBacktestContext:
    universe_id: str
    exchange: str
    symbol: str
    start: datetime
    end: datetime
    policy: PointInTimePolicy
    lifecycle_id: str
    membership_ids: tuple[str, ...]
    availability_ids: tuple[str, ...]
    delisting_id: str | None = None

    def __post_init__(self) -> None:
        _require_nonblank(self.universe_id, "Universe ID")
        object.__setattr__(
            self,
            "exchange",
            _normalized_code(self.exchange, "Exchange"),
        )
        object.__setattr__(self, "symbol", _normalized_code(self.symbol, "Symbol"))
        _require_aware(self.start, "Context start")
        _require_aware(self.end, "Context end")
        if self.start >= self.end:
            raise PointInTimeConfigurationError(
                "Point-in-time backtest context requires start before end."
            )
        if not isinstance(self.policy, PointInTimePolicy):
            raise PointInTimeConfigurationError(
                "Context policy must be a PointInTimePolicy."
            )
        _validate_uuid(self.lifecycle_id, "Lifecycle ID")
        _validate_sorted_unique_uuids(self.membership_ids, "Membership IDs")
        _validate_sorted_unique_uuids(self.availability_ids, "Availability IDs")
        if not self.membership_ids:
            raise PointInTimeConfigurationError(
                "Backtest context requires membership coverage."
            )
        if not self.availability_ids:
            raise PointInTimeConfigurationError(
                "Backtest context requires data-availability records."
            )
        if self.delisting_id is not None:
            _validate_uuid(self.delisting_id, "Delisting ID")

    @property
    def context_digest(self) -> str:
        return _sha256(self.to_document())

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "universe_id": self.universe_id,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "start": format_utc(self.start),
            "end": format_utc(self.end),
            "policy": self.policy.to_document(),
            "lifecycle_id": self.lifecycle_id,
            "membership_ids": list(self.membership_ids),
            "availability_ids": list(self.availability_ids),
            "delisting_id": self.delisting_id,
        }


@dataclass(frozen=True, slots=True)
class PointInTimeAccessGrant:
    data_id: str
    exchange: str
    symbol: str
    event_at: datetime
    decision_at: datetime
    lifecycle_id: str
    membership_id: str
    availability_id: str

    def __post_init__(self) -> None:
        _require_nonblank(self.data_id, "Data ID")
        object.__setattr__(
            self,
            "exchange",
            _normalized_code(self.exchange, "Exchange"),
        )
        object.__setattr__(self, "symbol", _normalized_code(self.symbol, "Symbol"))
        _require_aware(self.event_at, "Access event timestamp")
        _require_aware(self.decision_at, "Access decision timestamp")
        if self.decision_at < self.event_at:
            raise PointInTimeConfigurationError(
                "Decision timestamp cannot precede the data event."
            )
        _validate_uuid(self.lifecycle_id, "Lifecycle ID")
        _validate_uuid(self.membership_id, "Membership ID")
        _validate_uuid(self.availability_id, "Availability ID")

    @property
    def grant_digest(self) -> str:
        return _sha256(self.to_document())

    def to_document(self) -> dict[str, object]:
        return {
            "data_id": self.data_id,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "event_at": format_utc(self.event_at),
            "decision_at": format_utc(self.decision_at),
            "lifecycle_id": self.lifecycle_id,
            "membership_id": self.membership_id,
            "availability_id": self.availability_id,
        }


def point_in_time_policy_json(policy: PointInTimePolicy) -> str:
    if not isinstance(policy, PointInTimePolicy):
        raise PointInTimeConfigurationError(
            "Point-in-time policy JSON requires a PointInTimePolicy."
        )
    return canonical_json_bytes(policy.to_document()).decode("utf-8")


def security_lifecycle_from_document(
    document: Mapping[str, object],
) -> SecurityLifecycle:
    _require_schema(document)
    return SecurityLifecycle(
        exchange=_required_string(document, "exchange"),
        symbol=_required_string(document, "symbol"),
        listed_at=_parse_utc(_required_string(document, "listed_at")),
        tradable_from=_parse_utc(_required_string(document, "tradable_from")),
        delisted_at=_optional_datetime(document, "delisted_at"),
        tradable_until=_optional_datetime(document, "tradable_until"),
        source=_required_string(document, "source"),
        source_digest=_required_string(document, "source_digest"),
    )


def universe_membership_from_document(
    document: Mapping[str, object],
) -> UniverseMembership:
    _require_schema(document)
    return UniverseMembership(
        universe_id=_required_string(document, "universe_id"),
        exchange=_required_string(document, "exchange"),
        symbol=_required_string(document, "symbol"),
        member_from=_parse_utc(_required_string(document, "member_from")),
        member_until=_optional_datetime(document, "member_until"),
        available_at=_parse_utc(_required_string(document, "available_at")),
        source=_required_string(document, "source"),
        source_digest=_required_string(document, "source_digest"),
    )


def data_availability_from_document(
    document: Mapping[str, object],
) -> DataAvailabilityRecord:
    _require_schema(document)
    return DataAvailabilityRecord(
        data_id=_required_string(document, "data_id"),
        data_kind=PointInTimeDataKind(_required_string(document, "data_kind")),
        exchange=_required_string(document, "exchange"),
        symbol=_required_string(document, "symbol"),
        effective_at=_parse_utc(_required_string(document, "effective_at")),
        available_at=_parse_utc(_required_string(document, "available_at")),
        source=_required_string(document, "source"),
        source_digest=_required_string(document, "source_digest"),
    )


def delisting_record_from_document(
    document: Mapping[str, object],
) -> DelistingRecord:
    _require_schema(document)
    return DelistingRecord(
        exchange=_required_string(document, "exchange"),
        symbol=_required_string(document, "symbol"),
        last_tradable_at=_parse_utc(
            _required_string(document, "last_tradable_at")
        ),
        delisted_at=_parse_utc(_required_string(document, "delisted_at")),
        available_at=_parse_utc(_required_string(document, "available_at")),
        reason=DelistingReason(_required_string(document, "reason")),
        source=_required_string(document, "source"),
        source_digest=_required_string(document, "source_digest"),
    )


def _record_id(record_kind: str, document: Mapping[str, object]) -> str:
    digest = _sha256({"record_kind": record_kind, "record": document})
    return str(uuid5(_RECORD_NAMESPACE, digest))


def _sha256(document: object) -> str:
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


def _normalized_code(value: object, field_name: str) -> str:
    _require_nonblank(value, field_name)
    return cast(str, value).strip().upper()


def _validate_sha256(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise PointInTimeConfigurationError(
            f"{field_name} must be a lowercase SHA-256 digest."
        )


def _validate_uuid(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise PointInTimeConfigurationError(f"{field_name} must be a UUID string.")
    try:
        UUID(value)
    except ValueError as error:
        raise PointInTimeConfigurationError(
            f"{field_name} must be a valid UUID."
        ) from error


def _validate_sorted_unique_uuids(values: object, field_name: str) -> None:
    if not isinstance(values, tuple):
        raise PointInTimeConfigurationError(f"{field_name} must be a tuple.")
    for value in values:
        _validate_uuid(value, field_name)
    if tuple(sorted(values)) != values or len(values) != len(set(values)):
        raise PointInTimeConfigurationError(
            f"{field_name} must be uniquely and deterministically sorted."
        )


def _require_aware(value: object, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise PointInTimeConfigurationError(
            f"{field_name} must include timezone information."
        )


def _require_optional_aware(value: object, field_name: str) -> None:
    if value is not None:
        _require_aware(value, field_name)


def _require_nonblank(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise PointInTimeConfigurationError(f"{field_name} cannot be empty.")


def _require_schema(document: Mapping[str, object]) -> None:
    version = document.get("schema_version")
    if version != _SCHEMA_VERSION:
        raise PointInTimeIntegrityError(
            f"Unsupported point-in-time schema version: {version!r}."
        )


def _required_string(document: Mapping[str, object], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str):
        raise PointInTimeIntegrityError(f"Stored field {key!r} must be a string.")
    return value


def _optional_datetime(document: Mapping[str, object], key: str) -> datetime | None:
    value = document.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise PointInTimeIntegrityError(
            f"Stored field {key!r} must be a timestamp or null."
        )
    return _parse_utc(value)


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PointInTimeIntegrityError(
            "Stored point-in-time timestamp is invalid."
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PointInTimeIntegrityError(
            "Stored point-in-time timestamps must include timezone information."
        )
    return parsed.astimezone(UTC)


__all__ = [
    "AsOfPolicy",
    "AvailabilityPolicy",
    "DataAvailabilityRecord",
    "DelistingPolicy",
    "DelistingReason",
    "DelistingRecord",
    "PointInTimeAccessGrant",
    "PointInTimeBacktestContext",
    "PointInTimeConfigurationError",
    "PointInTimeConflictError",
    "PointInTimeDataKind",
    "PointInTimeEligibilityError",
    "PointInTimeError",
    "PointInTimeIntegrityError",
    "PointInTimeMember",
    "PointInTimeNotFoundError",
    "PointInTimeOverlapError",
    "PointInTimePolicy",
    "PointInTimeSnapshot",
    "SecurityLifecycle",
    "UniverseMembership",
    "data_availability_from_document",
    "delisting_record_from_document",
    "point_in_time_policy_json",
    "security_lifecycle_from_document",
    "universe_membership_from_document",
]
