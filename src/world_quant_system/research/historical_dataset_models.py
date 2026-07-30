from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import cast
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from world_quant_system.data.normalized_models import canonical_json_bytes, format_utc
from world_quant_system.data.quality_models import QualityStatus
from world_quant_system.domain import CandleInterval
from world_quant_system.research.models import ResearchError

_SCHEMA_VERSION = 1
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")
_SEMVER_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_PROVIDER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_DATASET_NAMESPACE = UUID("c6d23074-8069-588e-b225-93dfd1c42939")
_BENCHMARK_NAMESPACE = UUID("47a6f547-acde-5b04-a1a9-b741d44bbc3a")


class HistoricalDatasetError(ResearchError):
    """Base exception for historical-dataset failures."""


class HistoricalDatasetConfigurationError(HistoricalDatasetError):
    """Raised when historical-dataset configuration is invalid."""


class HistoricalDatasetConflictError(HistoricalDatasetError):
    """Raised when immutable historical-dataset records conflict."""


class HistoricalDatasetIntegrityError(HistoricalDatasetError):
    """Raised when historical-dataset storage fails integrity validation."""


class HistoricalDatasetNotFoundError(HistoricalDatasetError):
    """Raised when a historical dataset does not exist."""


class HistoricalDatasetFrozenError(HistoricalDatasetError):
    """Raised when a frozen historical dataset would be mutated."""


class HistoricalDatasetEligibilityError(HistoricalDatasetError):
    """Raised when source data cannot enter a trusted research snapshot."""


class HistoricalDatasetFormat(StrEnum):
    CSV = "csv"


class HistoricalDatasetState(StrEnum):
    IMPORTED = "imported"
    FROZEN = "frozen"


class MissingSessionPolicy(StrEnum):
    WARN = "warn"
    REJECT = "reject"


class HistoricalDatasetIssueCode(StrEnum):
    MISSING_SESSION = "missing_session"


@dataclass(frozen=True, slots=True)
class HistoricalDatasetIssue:
    code: HistoricalDatasetIssueCode
    message: str
    session_date: date | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, HistoricalDatasetIssueCode):
            raise HistoricalDatasetConfigurationError(
                "Historical dataset issue code is invalid."
            )
        _require_nonblank(self.message, "Historical dataset issue message")
        if self.session_date is not None and (
            not isinstance(self.session_date, date)
            or isinstance(self.session_date, datetime)
        ):
            raise HistoricalDatasetConfigurationError(
                "Historical dataset issue date must be a date or None."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "message": self.message,
            "session_date": (
                None if self.session_date is None else self.session_date.isoformat()
            ),
        }


@dataclass(frozen=True, slots=True)
class HistoricalDatasetPolicy:
    timezone: str
    missing_session_policy: MissingSessionPolicy = MissingSessionPolicy.REJECT
    expected_weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)
    holidays: tuple[date, ...] = ()

    def __post_init__(self) -> None:
        _validate_timezone(self.timezone)
        if not isinstance(self.missing_session_policy, MissingSessionPolicy):
            raise HistoricalDatasetConfigurationError(
                "Missing-session policy is invalid."
            )
        if (
            not isinstance(self.expected_weekdays, tuple)
            or not self.expected_weekdays
            or len(set(self.expected_weekdays)) != len(self.expected_weekdays)
            or tuple(sorted(self.expected_weekdays)) != self.expected_weekdays
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= 6
                for value in self.expected_weekdays
            )
        ):
            raise HistoricalDatasetConfigurationError(
                "Expected weekdays must be a non-empty tuple of unique integers 0-6."
            )
        if not isinstance(self.holidays, tuple) or any(
            not isinstance(value, date) or isinstance(value, datetime)
            for value in self.holidays
        ):
            raise HistoricalDatasetConfigurationError(
                "Holidays must be stored as a tuple of dates."
            )
        if tuple(sorted(set(self.holidays))) != self.holidays:
            raise HistoricalDatasetConfigurationError(
                "Holidays must be unique and sorted."
            )

    @property
    def fingerprint(self) -> str:
        return _sha256(self.to_document())

    def to_document(self) -> dict[str, object]:
        return {
            "timezone": self.timezone,
            "missing_session_policy": self.missing_session_policy.value,
            "expected_weekdays": list(self.expected_weekdays),
            "holidays": [value.isoformat() for value in self.holidays],
        }


@dataclass(frozen=True, slots=True)
class HistoricalDatasetImportSpec:
    provider: str
    exchange: str
    symbol: str
    interval: CandleInterval
    currency: str
    code_commit: str
    point_in_time_context_digest: str
    corporate_action_context_digest: str
    policy: HistoricalDatasetPolicy
    source_format: HistoricalDatasetFormat = HistoricalDatasetFormat.CSV

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not _PROVIDER_PATTERN.fullmatch(
            self.provider
        ):
            raise HistoricalDatasetConfigurationError(
                "Provider must be a lowercase storage-safe identifier."
            )
        _require_code(self.exchange, "Exchange")
        _require_code(self.symbol, "Symbol")
        if not isinstance(self.interval, CandleInterval):
            raise HistoricalDatasetConfigurationError(
                "Interval must be a CandleInterval value."
            )
        _validate_currency(self.currency)
        if not isinstance(self.code_commit, str) or not _GIT_COMMIT_PATTERN.fullmatch(
            self.code_commit
        ):
            raise HistoricalDatasetConfigurationError(
                "Code commit must be a lowercase hexadecimal Git object ID."
            )
        _validate_sha256(
            self.point_in_time_context_digest,
            "Point-in-time context digest",
        )
        _validate_sha256(
            self.corporate_action_context_digest,
            "Corporate-action context digest",
        )
        if not isinstance(self.policy, HistoricalDatasetPolicy):
            raise HistoricalDatasetConfigurationError(
                "Historical dataset policy is invalid."
            )
        if not isinstance(self.source_format, HistoricalDatasetFormat):
            raise HistoricalDatasetConfigurationError(
                "Historical dataset source format is invalid."
            )

    def identity_document(self, source_sha256: str) -> dict[str, object]:
        _validate_sha256(source_sha256, "Source SHA-256")
        return {
            "schema_version": _SCHEMA_VERSION,
            "provider": self.provider,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "interval": self.interval.value,
            "currency": self.currency,
            "code_commit": self.code_commit,
            "point_in_time_context_digest": self.point_in_time_context_digest,
            "corporate_action_context_digest": (
                self.corporate_action_context_digest
            ),
            "policy": self.policy.to_document(),
            "source_format": self.source_format.value,
            "source_sha256": source_sha256,
        }

    def dataset_id(self, source_sha256: str) -> str:
        identity_digest = _sha256(self.identity_document(source_sha256))
        return str(uuid5(_DATASET_NAMESPACE, identity_digest))


@dataclass(frozen=True, slots=True)
class HistoricalDatasetManifest:
    dataset_id: str
    provider: str
    exchange: str
    symbols: tuple[str, ...]
    interval: CandleInterval
    timezone: str
    currency: str
    start: datetime
    end: datetime
    item_count: int
    imported_at: datetime
    source_format: HistoricalDatasetFormat
    source_name: str
    source_sha256: str
    raw_record_id: str
    raw_content_sha256: str
    quality_report_id: str
    quality_status: QualityStatus
    quality_policy_digest: str
    normalizer_version: str
    normalized_digest: str
    point_in_time_context_digest: str
    corporate_action_context_digest: str
    code_commit: str
    policy: HistoricalDatasetPolicy
    issues: tuple[HistoricalDatasetIssue, ...] = ()
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_uuid(self.dataset_id, "Dataset ID")
        if not isinstance(self.provider, str) or not _PROVIDER_PATTERN.fullmatch(
            self.provider
        ):
            raise HistoricalDatasetConfigurationError(
                "Stored provider is invalid."
            )
        _require_code(self.exchange, "Exchange")
        if (
            not isinstance(self.symbols, tuple)
            or len(self.symbols) != 1
            or tuple(sorted(set(self.symbols))) != self.symbols
        ):
            raise HistoricalDatasetConfigurationError(
                "Historical Dataset v1 requires exactly one unique symbol."
            )
        for symbol in self.symbols:
            _require_code(symbol, "Dataset symbol")
        if not isinstance(self.interval, CandleInterval):
            raise HistoricalDatasetConfigurationError(
                "Stored interval is invalid."
            )
        _validate_timezone(self.timezone)
        _validate_currency(self.currency)
        _require_aware(self.start, "Dataset start")
        _require_aware(self.end, "Dataset end")
        if self.start > self.end:
            raise HistoricalDatasetConfigurationError(
                "Dataset start cannot be later than dataset end."
            )
        _require_positive_int(self.item_count, "Dataset item count")
        _require_aware(self.imported_at, "Dataset imported-at timestamp")
        if self.imported_at < self.end:
            raise HistoricalDatasetConfigurationError(
                "Dataset import timestamp cannot precede the final observation."
            )
        if not isinstance(self.source_format, HistoricalDatasetFormat):
            raise HistoricalDatasetConfigurationError(
                "Stored source format is invalid."
            )
        _require_nonblank(self.source_name, "Source name")
        if "/" in self.source_name or "\\" in self.source_name:
            raise HistoricalDatasetConfigurationError(
                "Source name must not contain path separators."
            )
        for value, name in (
            (self.source_sha256, "Source SHA-256"),
            (self.raw_content_sha256, "Raw content SHA-256"),
            (self.quality_policy_digest, "Quality policy digest"),
            (self.normalized_digest, "Normalized digest"),
            (self.point_in_time_context_digest, "Point-in-time context digest"),
            (
                self.corporate_action_context_digest,
                "Corporate-action context digest",
            ),
        ):
            _validate_sha256(value, name)
        _validate_uuid(self.raw_record_id, "Raw record ID")
        _validate_uuid(self.quality_report_id, "Quality report ID")
        if not isinstance(self.quality_status, QualityStatus):
            raise HistoricalDatasetConfigurationError(
                "Stored quality status is invalid."
            )
        if self.quality_status is QualityStatus.QUARANTINE:
            raise HistoricalDatasetEligibilityError(
                "Quarantined data cannot be stored as a historical dataset."
            )
        if (
            not isinstance(self.normalizer_version, str)
            or not _SEMVER_PATTERN.fullmatch(self.normalizer_version)
        ):
            raise HistoricalDatasetConfigurationError(
                "Normalizer version must use semantic x.y.z form."
            )
        if not isinstance(self.code_commit, str) or not _GIT_COMMIT_PATTERN.fullmatch(
            self.code_commit
        ):
            raise HistoricalDatasetConfigurationError(
                "Stored code commit is invalid."
            )
        if not isinstance(self.policy, HistoricalDatasetPolicy):
            raise HistoricalDatasetConfigurationError(
                "Stored historical dataset policy is invalid."
            )
        if self.timezone != self.policy.timezone:
            raise HistoricalDatasetConfigurationError(
                "Manifest timezone must match its policy."
            )
        if not isinstance(self.issues, tuple) or any(
            not isinstance(issue, HistoricalDatasetIssue) for issue in self.issues
        ):
            raise HistoricalDatasetConfigurationError(
                "Historical dataset issues must be a tuple."
            )
        if self.schema_version != _SCHEMA_VERSION:
            raise HistoricalDatasetConfigurationError(
                "Historical dataset schema version is unsupported."
            )
        expected_dataset_id = HistoricalDatasetImportSpec(
            provider=self.provider,
            exchange=self.exchange,
            symbol=self.symbols[0],
            interval=self.interval,
            currency=self.currency,
            code_commit=self.code_commit,
            point_in_time_context_digest=self.point_in_time_context_digest,
            corporate_action_context_digest=(
                self.corporate_action_context_digest
            ),
            policy=self.policy,
            source_format=self.source_format,
        ).dataset_id(self.source_sha256)
        if self.dataset_id != expected_dataset_id:
            raise HistoricalDatasetConfigurationError(
                "Dataset ID does not match its immutable import identity."
            )

    @property
    def manifest_digest(self) -> str:
        return _sha256(self.to_document())

    @property
    def dataset_digest(self) -> str:
        return _sha256(
            {
                "schema_version": self.schema_version,
                "dataset_id": self.dataset_id,
                "provider": self.provider,
                "exchange": self.exchange,
                "symbols": list(self.symbols),
                "interval": self.interval.value,
                "timezone": self.timezone,
                "currency": self.currency,
                "start": format_utc(self.start),
                "end": format_utc(self.end),
                "item_count": self.item_count,
                "source_format": self.source_format.value,
                "source_sha256": self.source_sha256,
                "quality_status": self.quality_status.value,
                "quality_policy_digest": self.quality_policy_digest,
                "normalizer_version": self.normalizer_version,
                "normalized_digest": self.normalized_digest,
                "point_in_time_context_digest": (
                    self.point_in_time_context_digest
                ),
                "corporate_action_context_digest": (
                    self.corporate_action_context_digest
                ),
                "code_commit": self.code_commit,
                "policy": self.policy.to_document(),
                "issues": [issue.to_document() for issue in self.issues],
            }
        )

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "dataset_id": self.dataset_id,
            "provider": self.provider,
            "exchange": self.exchange,
            "symbols": list(self.symbols),
            "interval": self.interval.value,
            "timezone": self.timezone,
            "currency": self.currency,
            "start": format_utc(self.start),
            "end": format_utc(self.end),
            "item_count": self.item_count,
            "imported_at": format_utc(self.imported_at),
            "source_format": self.source_format.value,
            "source_name": self.source_name,
            "source_sha256": self.source_sha256,
            "raw_record_id": self.raw_record_id,
            "raw_content_sha256": self.raw_content_sha256,
            "quality_report_id": self.quality_report_id,
            "quality_status": self.quality_status.value,
            "quality_policy_digest": self.quality_policy_digest,
            "normalizer_version": self.normalizer_version,
            "normalized_digest": self.normalized_digest,
            "point_in_time_context_digest": self.point_in_time_context_digest,
            "corporate_action_context_digest": (
                self.corporate_action_context_digest
            ),
            "code_commit": self.code_commit,
            "policy": self.policy.to_document(),
            "issues": [issue.to_document() for issue in self.issues],
        }


@dataclass(frozen=True, slots=True)
class HistoricalDatasetSnapshot:
    manifest: HistoricalDatasetManifest
    state: HistoricalDatasetState
    frozen_at: datetime | None

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, HistoricalDatasetManifest):
            raise HistoricalDatasetConfigurationError(
                "Historical dataset snapshot requires a manifest."
            )
        if not isinstance(self.state, HistoricalDatasetState):
            raise HistoricalDatasetConfigurationError(
                "Historical dataset state is invalid."
            )
        if self.state is HistoricalDatasetState.IMPORTED and self.frozen_at is not None:
            raise HistoricalDatasetConfigurationError(
                "Imported datasets cannot have a frozen-at timestamp."
            )
        if self.state is HistoricalDatasetState.FROZEN:
            if self.frozen_at is None:
                raise HistoricalDatasetConfigurationError(
                    "Frozen datasets require a frozen-at timestamp."
                )
            _require_aware(self.frozen_at, "Frozen-at timestamp")
            if self.frozen_at < self.manifest.imported_at:
                raise HistoricalDatasetConfigurationError(
                    "Frozen-at timestamp cannot precede dataset import."
                )

    @property
    def dataset_digest(self) -> str:
        return self.manifest.dataset_digest


@dataclass(frozen=True, slots=True)
class BenchmarkLink:
    link_id: str
    dataset_id: str
    benchmark_dataset_id: str
    alignment_digest: str
    created_at: datetime

    def __post_init__(self) -> None:
        _validate_uuid(self.link_id, "Benchmark link ID")
        _validate_uuid(self.dataset_id, "Dataset ID")
        _validate_uuid(self.benchmark_dataset_id, "Benchmark dataset ID")
        if self.dataset_id == self.benchmark_dataset_id:
            raise HistoricalDatasetConfigurationError(
                "A dataset cannot benchmark itself."
            )
        _validate_sha256(self.alignment_digest, "Benchmark alignment digest")
        _require_aware(self.created_at, "Benchmark link creation timestamp")

    @classmethod
    def build(
        cls,
        *,
        dataset: HistoricalDatasetManifest,
        benchmark: HistoricalDatasetManifest,
        created_at: datetime,
    ) -> BenchmarkLink:
        _require_aware(created_at, "Benchmark link creation timestamp")
        if dataset.interval is not benchmark.interval:
            raise HistoricalDatasetEligibilityError(
                "Benchmark interval must match the strategy dataset."
            )
        if dataset.start != benchmark.start or dataset.end != benchmark.end:
            raise HistoricalDatasetEligibilityError(
                "Benchmark time range must match the strategy dataset."
            )
        if dataset.timezone != benchmark.timezone:
            raise HistoricalDatasetEligibilityError(
                "Benchmark timezone must match the strategy dataset."
            )
        if dataset.currency != benchmark.currency:
            raise HistoricalDatasetEligibilityError(
                "Benchmark currency must match the strategy dataset."
            )
        alignment_document = {
            "dataset_digest": dataset.dataset_digest,
            "benchmark_digest": benchmark.dataset_digest,
            "interval": dataset.interval.value,
            "start": format_utc(dataset.start),
            "end": format_utc(dataset.end),
            "timezone": dataset.timezone,
            "currency": dataset.currency,
        }
        alignment_digest = _sha256(alignment_document)
        link_id = str(uuid5(_BENCHMARK_NAMESPACE, alignment_digest))
        return cls(
            link_id=link_id,
            dataset_id=dataset.dataset_id,
            benchmark_dataset_id=benchmark.dataset_id,
            alignment_digest=alignment_digest,
            created_at=created_at.astimezone(UTC),
        )


def historical_dataset_manifest_from_document(
    document: object,
) -> HistoricalDatasetManifest:
    if not isinstance(document, dict):
        raise HistoricalDatasetIntegrityError(
            "Historical dataset manifest must be a JSON object."
        )
    value = cast(dict[str, object], document)
    try:
        policy_document = _required_dict(value, "policy")
        policy = HistoricalDatasetPolicy(
            timezone=_required_str(policy_document, "timezone"),
            missing_session_policy=MissingSessionPolicy(
                _required_str(policy_document, "missing_session_policy")
            ),
            expected_weekdays=tuple(
                _required_int(item, "Expected weekday")
                for item in _required_list(policy_document, "expected_weekdays")
            ),
            holidays=tuple(
                date.fromisoformat(_required_str_value(item, "Holiday"))
                for item in _required_list(policy_document, "holidays")
            ),
        )
        issues = tuple(
            _issue_from_document(item)
            for item in _required_list(value, "issues")
        )
        return HistoricalDatasetManifest(
            dataset_id=_required_str(value, "dataset_id"),
            provider=_required_str(value, "provider"),
            exchange=_required_str(value, "exchange"),
            symbols=tuple(
                _required_str_value(item, "Dataset symbol")
                for item in _required_list(value, "symbols")
            ),
            interval=CandleInterval(_required_str(value, "interval")),
            timezone=_required_str(value, "timezone"),
            currency=_required_str(value, "currency"),
            start=_parse_utc(_required_str(value, "start")),
            end=_parse_utc(_required_str(value, "end")),
            item_count=_required_int(value.get("item_count"), "Dataset item count"),
            imported_at=_parse_utc(_required_str(value, "imported_at")),
            source_format=HistoricalDatasetFormat(
                _required_str(value, "source_format")
            ),
            source_name=_required_str(value, "source_name"),
            source_sha256=_required_str(value, "source_sha256"),
            raw_record_id=_required_str(value, "raw_record_id"),
            raw_content_sha256=_required_str(value, "raw_content_sha256"),
            quality_report_id=_required_str(value, "quality_report_id"),
            quality_status=QualityStatus(_required_str(value, "quality_status")),
            quality_policy_digest=_required_str(value, "quality_policy_digest"),
            normalizer_version=_required_str(value, "normalizer_version"),
            normalized_digest=_required_str(value, "normalized_digest"),
            point_in_time_context_digest=_required_str(
                value, "point_in_time_context_digest"
            ),
            corporate_action_context_digest=_required_str(
                value, "corporate_action_context_digest"
            ),
            code_commit=_required_str(value, "code_commit"),
            policy=policy,
            issues=issues,
            schema_version=_required_int(
                value.get("schema_version"), "Schema version"
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise HistoricalDatasetIntegrityError(
            "Historical dataset manifest contains invalid values."
        ) from error


def canonical_manifest_json(manifest: HistoricalDatasetManifest) -> str:
    return canonical_json_bytes(manifest.to_document()).decode("utf-8")


def _issue_from_document(value: object) -> HistoricalDatasetIssue:
    if not isinstance(value, dict):
        raise HistoricalDatasetIntegrityError(
            "Historical dataset issue must be a JSON object."
        )
    item = cast(dict[str, object], value)
    session_value = item.get("session_date")
    return HistoricalDatasetIssue(
        code=HistoricalDatasetIssueCode(_required_str(item, "code")),
        message=_required_str(item, "message"),
        session_date=(
            None
            if session_value is None
            else date.fromisoformat(_required_str_value(session_value, "Session date"))
        ),
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _validate_sha256(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise HistoricalDatasetConfigurationError(
            f"{field_name} must be a lowercase SHA-256 digest."
        )


def _validate_uuid(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise HistoricalDatasetConfigurationError(
            f"{field_name} must be a canonical UUID string."
        )
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise HistoricalDatasetConfigurationError(
            f"{field_name} must be a canonical UUID string."
        ) from error
    if str(parsed) != value:
        raise HistoricalDatasetConfigurationError(
            f"{field_name} must be a canonical UUID string."
        )


def _validate_timezone(value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise HistoricalDatasetConfigurationError(
            "Timezone must be a nonblank IANA timezone name."
        )
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise HistoricalDatasetConfigurationError(
            "Timezone must be a valid IANA timezone name."
        ) from error


def _validate_currency(value: object) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 3
        or not value.isascii()
        or not value.isalpha()
        or value != value.upper()
    ):
        raise HistoricalDatasetConfigurationError(
            "Currency must be an uppercase three-letter code."
        )


def _require_code(value: object, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip().upper()
        or len(value) > 64
    ):
        raise HistoricalDatasetConfigurationError(
            f"{field_name} must be a nonblank uppercase code."
        )


def _require_nonblank(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise HistoricalDatasetConfigurationError(
            f"{field_name} must be a bounded nonblank string."
        )


def _require_positive_int(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HistoricalDatasetConfigurationError(
            f"{field_name} must be a positive integer."
        )


def _require_aware(value: object, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise HistoricalDatasetConfigurationError(
            f"{field_name} must include timezone information."
        )


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    _require_aware(parsed, "Stored timestamp")
    return parsed.astimezone(UTC)


def _required_dict(value: dict[str, object], key: str) -> dict[str, object]:
    item = value[key]
    if not isinstance(item, dict):
        raise HistoricalDatasetIntegrityError(f"{key} must be a JSON object.")
    return cast(dict[str, object], item)


def _required_list(value: dict[str, object], key: str) -> list[object]:
    item = value[key]
    if not isinstance(item, list):
        raise HistoricalDatasetIntegrityError(f"{key} must be a JSON array.")
    return cast(list[object], item)


def _required_str(value: dict[str, object], key: str) -> str:
    return _required_str_value(value[key], key)


def _required_str_value(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise HistoricalDatasetIntegrityError(f"{field_name} must be a string.")
    return value


def _required_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HistoricalDatasetIntegrityError(f"{field_name} must be an integer.")
    return value


__all__ = [
    "BenchmarkLink",
    "HistoricalDatasetConfigurationError",
    "HistoricalDatasetConflictError",
    "HistoricalDatasetEligibilityError",
    "HistoricalDatasetError",
    "HistoricalDatasetFormat",
    "HistoricalDatasetFrozenError",
    "HistoricalDatasetImportSpec",
    "HistoricalDatasetIntegrityError",
    "HistoricalDatasetIssue",
    "HistoricalDatasetIssueCode",
    "HistoricalDatasetManifest",
    "HistoricalDatasetNotFoundError",
    "HistoricalDatasetPolicy",
    "HistoricalDatasetSnapshot",
    "HistoricalDatasetState",
    "MissingSessionPolicy",
    "canonical_manifest_json",
    "historical_dataset_manifest_from_document",
]
