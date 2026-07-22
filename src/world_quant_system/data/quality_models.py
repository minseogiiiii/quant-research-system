from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, cast
from uuid import UUID

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_MAX_MESSAGE_LENGTH = 512
_MAX_DETAIL_ITEMS = 16
_MAX_DETAIL_LENGTH = 256


class DataQualityError(Exception):
    """Base exception for market-data quality failures."""


class DataQualityConfigurationError(DataQualityError):
    """Raised when quality-gate configuration is invalid."""


class DataQualityRejectedError(DataQualityError):
    """Raised when market data is quarantined and cannot be consumed."""

    def __init__(self, report_id: str) -> None:
        super().__init__(
            "Market data was quarantined by the data-quality gate "
            f"(report_id={report_id})."
        )
        self.report_id = report_id


class DataQualityStoreError(DataQualityError):
    """Base exception for quality-report persistence failures."""


class DataQualityStoreConflictError(DataQualityStoreError):
    """Raised when an assessment key is reused for different results."""


class DataQualityStoreIntegrityError(DataQualityStoreError):
    """Raised when stored quality-report bytes fail verification."""


class DataQualityReportNotFoundError(DataQualityStoreError):
    """Raised when a requested quality report does not exist."""


class QualityStatus(StrEnum):
    PASS = "PASS"
    WARNING = "WARNING"
    QUARANTINE = "QUARANTINE"


class QualitySeverity(StrEnum):
    WARNING = "WARNING"
    QUARANTINE = "QUARANTINE"


class QualityDatasetKind(StrEnum):
    QUOTES = "quotes"
    CANDLES = "candles"
    PARSE_FAILURE = "parse_failure"


class QualityIssueCode(StrEnum):
    RAW_INTEGRITY_FAILURE = "raw_integrity_failure"
    RAW_METADATA_MISMATCH = "raw_metadata_mismatch"
    ENDPOINT_MISMATCH = "endpoint_mismatch"
    PARSE_FAILURE = "parse_failure"
    FUTURE_TIMESTAMP = "future_timestamp"
    STALE_QUOTE = "stale_quote"
    SOURCE_MISMATCH = "source_mismatch"
    EMPTY_DATASET = "empty_dataset"
    CANDLE_GAP = "candle_gap"
    EXTREME_PRICE_MOVE = "extreme_price_move"
    WIDE_CANDLE_RANGE = "wide_candle_range"
    ZERO_VOLUME_RUN = "zero_volume_run"
    FLATLINE_RUN = "flatline_run"


@dataclass(frozen=True, slots=True)
class QualityIssue:
    code: QualityIssueCode
    severity: QualitySeverity
    message: str
    item_index: int | None = None
    timestamp: datetime | None = None
    details: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.code, QualityIssueCode):
            raise DataQualityConfigurationError(
                "Quality issue code must be a QualityIssueCode value."
            )
        if not isinstance(self.severity, QualitySeverity):
            raise DataQualityConfigurationError(
                "Quality issue severity must be a QualitySeverity value."
            )
        if (
            not isinstance(self.message, str)
            or not self.message.strip()
            or len(self.message) > _MAX_MESSAGE_LENGTH
        ):
            raise DataQualityConfigurationError(
                "Quality issue message must be a nonblank bounded string."
            )
        if self.item_index is not None and (
            isinstance(self.item_index, bool)
            or not isinstance(self.item_index, int)
            or self.item_index < 0
        ):
            raise DataQualityConfigurationError(
                "Quality issue item index must be a nonnegative integer or None."
            )
        if self.timestamp is not None:
            _require_aware_datetime(self.timestamp, "Quality issue timestamp")
        if not isinstance(self.details, Mapping):
            raise DataQualityConfigurationError(
                "Quality issue details must be a mapping."
            )
        if len(self.details) > _MAX_DETAIL_ITEMS:
            raise DataQualityConfigurationError(
                "Quality issue details contain too many entries."
            )

        normalized_details: dict[str, str] = {}
        for key, value in self.details.items():
            if (
                not isinstance(key, str)
                or not key.strip()
                or len(key) > _MAX_DETAIL_LENGTH
                or not isinstance(value, str)
                or len(value) > _MAX_DETAIL_LENGTH
            ):
                raise DataQualityConfigurationError(
                    "Quality issue details must contain bounded string pairs."
                )
            normalized_details[key] = value

        object.__setattr__(
            self,
            "details",
            MappingProxyType(normalized_details),
        )


@dataclass(frozen=True, slots=True)
class DataQualityPolicy:
    future_tolerance: timedelta = timedelta(minutes=5)
    stale_quote_warning_after: timedelta = timedelta(minutes=15)
    stale_quote_quarantine_after: timedelta | None = None
    intraday_gap_warning_after: timedelta = timedelta(minutes=5)
    intraday_session_break_after: timedelta = timedelta(hours=8)
    daily_gap_warning_after: timedelta = timedelta(days=10)
    extreme_return_warning_ratio: Decimal = Decimal("0.50")
    wide_range_warning_ratio: Decimal = Decimal("0.50")
    zero_volume_run_warning: int = 3
    flatline_run_warning: int = 5
    max_issues: int = 1_000

    def __post_init__(self) -> None:
        for duration_name, duration_value in (
            ("Future tolerance", self.future_tolerance),
            ("Stale quote warning threshold", self.stale_quote_warning_after),
            ("Intraday gap warning threshold", self.intraday_gap_warning_after),
            ("Intraday session-break threshold", self.intraday_session_break_after),
            ("Daily gap warning threshold", self.daily_gap_warning_after),
        ):
            _require_nonnegative_timedelta(duration_value, duration_name)

        if self.stale_quote_quarantine_after is not None:
            _require_nonnegative_timedelta(
                self.stale_quote_quarantine_after,
                "Stale quote quarantine threshold",
            )
            if (
                self.stale_quote_quarantine_after
                < self.stale_quote_warning_after
            ):
                raise DataQualityConfigurationError(
                    "Stale quote quarantine threshold cannot be earlier than "
                    "the warning threshold."
                )

        if self.intraday_session_break_after <= self.intraday_gap_warning_after:
            raise DataQualityConfigurationError(
                "Intraday session-break threshold must exceed the gap warning "
                "threshold."
            )

        for ratio_name, ratio_value in (
            ("Extreme return warning ratio", self.extreme_return_warning_ratio),
            ("Wide range warning ratio", self.wide_range_warning_ratio),
        ):
            if (
                not isinstance(ratio_value, Decimal)
                or not ratio_value.is_finite()
                or ratio_value <= 0
            ):
                raise DataQualityConfigurationError(
                    f"{ratio_name} must be a finite positive Decimal."
                )

        for count_name, count_value, minimum in (
            ("Zero-volume run warning", self.zero_volume_run_warning, 1),
            ("Flatline run warning", self.flatline_run_warning, 2),
            ("Maximum issue count", self.max_issues, 1),
        ):
            if (
                isinstance(count_value, bool)
                or not isinstance(count_value, int)
                or count_value < minimum
            ):
                raise DataQualityConfigurationError(
                    f"{count_name} must be an integer of at least {minimum}."
                )

    @property
    def fingerprint(self) -> str:
        payload = {
            "future_tolerance_us": _timedelta_microseconds(self.future_tolerance),
            "stale_quote_warning_after_us": _timedelta_microseconds(
                self.stale_quote_warning_after
            ),
            "stale_quote_quarantine_after_us": (
                _timedelta_microseconds(self.stale_quote_quarantine_after)
                if self.stale_quote_quarantine_after is not None
                else None
            ),
            "intraday_gap_warning_after_us": _timedelta_microseconds(
                self.intraday_gap_warning_after
            ),
            "intraday_session_break_after_us": _timedelta_microseconds(
                self.intraday_session_break_after
            ),
            "daily_gap_warning_after_us": _timedelta_microseconds(
                self.daily_gap_warning_after
            ),
            "extreme_return_warning_ratio": format(
                self.extreme_return_warning_ratio,
                "f",
            ),
            "wide_range_warning_ratio": format(
                self.wide_range_warning_ratio,
                "f",
            ),
            "zero_volume_run_warning": self.zero_volume_run_warning,
            "flatline_run_warning": self.flatline_run_warning,
            "max_issues": self.max_issues,
        }
        return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class QualityAssessment:
    dataset_kind: QualityDatasetKind
    item_count: int
    issues: tuple[QualityIssue, ...]
    status: QualityStatus

    def __post_init__(self) -> None:
        if not isinstance(self.dataset_kind, QualityDatasetKind):
            raise DataQualityConfigurationError(
                "Dataset kind must be a QualityDatasetKind value."
            )
        if (
            isinstance(self.item_count, bool)
            or not isinstance(self.item_count, int)
            or self.item_count < 0
        ):
            raise DataQualityConfigurationError(
                "Assessment item count must be a nonnegative integer."
            )
        if not isinstance(self.issues, tuple) or not all(
            isinstance(issue, QualityIssue) for issue in self.issues
        ):
            raise DataQualityConfigurationError(
                "Assessment issues must be a tuple of QualityIssue values."
            )
        if not isinstance(self.status, QualityStatus):
            raise DataQualityConfigurationError(
                "Assessment status must be a QualityStatus value."
            )
        expected = status_from_issues(self.issues)
        if self.status is not expected:
            raise DataQualityConfigurationError(
                "Assessment status does not match issue severities."
            )


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    report_id: str
    assessment_key: str
    record_id: str
    raw_content_sha256: str
    dataset_kind: QualityDatasetKind
    status: QualityStatus
    checked_at: datetime
    validator_version: str
    policy_fingerprint: str
    item_count: int
    issues: tuple[QualityIssue, ...]

    def __post_init__(self) -> None:
        _validate_uuid(self.report_id, "Report ID")
        _validate_sha256(self.assessment_key, "Assessment key")
        _validate_uuid(self.record_id, "Record ID")
        _validate_sha256(self.raw_content_sha256, "Raw content SHA-256")
        if not isinstance(self.dataset_kind, QualityDatasetKind):
            raise DataQualityConfigurationError(
                "Dataset kind must be a QualityDatasetKind value."
            )
        if not isinstance(self.status, QualityStatus):
            raise DataQualityConfigurationError(
                "Report status must be a QualityStatus value."
            )
        _require_aware_datetime(self.checked_at, "Quality check timestamp")
        if (
            not isinstance(self.validator_version, str)
            or not _VERSION_PATTERN.fullmatch(self.validator_version)
        ):
            raise DataQualityConfigurationError(
                "Validator version must use semantic x.y.z form."
            )
        _validate_sha256(self.policy_fingerprint, "Policy fingerprint")
        if (
            isinstance(self.item_count, bool)
            or not isinstance(self.item_count, int)
            or self.item_count < 0
        ):
            raise DataQualityConfigurationError(
                "Report item count must be a nonnegative integer."
            )
        if not isinstance(self.issues, tuple) or not all(
            isinstance(issue, QualityIssue) for issue in self.issues
        ):
            raise DataQualityConfigurationError(
                "Report issues must be a tuple of QualityIssue values."
            )
        if self.status is not status_from_issues(self.issues):
            raise DataQualityConfigurationError(
                "Report status does not match issue severities."
            )

    @property
    def is_usable(self) -> bool:
        return self.status is not QualityStatus.QUARANTINE


class DataQualityReportWriter(Protocol):
    async def save(self, report: DataQualityReport) -> DataQualityReport:
        """Persist a report idempotently and return the stored report."""
        ...


class DataQualityReportReader(Protocol):
    async def get(self, report_id: str) -> DataQualityReport:
        """Read and verify one report."""
        ...

    async def query(
        self,
        *,
        status: QualityStatus | None = None,
        record_id: str | None = None,
        limit: int = 1_000,
    ) -> tuple[DataQualityReport, ...]:
        """Return reports in deterministic check-time order."""
        ...


def status_from_issues(issues: Sequence[QualityIssue]) -> QualityStatus:
    if any(
        issue.severity is QualitySeverity.QUARANTINE
        for issue in issues
    ):
        return QualityStatus.QUARANTINE
    if issues:
        return QualityStatus.WARNING
    return QualityStatus.PASS


def build_assessment_key(
    *,
    record_id: str,
    raw_content_sha256: str,
    dataset_kind: QualityDatasetKind,
    validator_version: str,
    policy_fingerprint: str,
) -> str:
    _validate_uuid(record_id, "Record ID")
    _validate_sha256(raw_content_sha256, "Raw content SHA-256")
    if not isinstance(dataset_kind, QualityDatasetKind):
        raise DataQualityConfigurationError(
            "Dataset kind must be a QualityDatasetKind value."
        )
    if not isinstance(validator_version, str) or not _VERSION_PATTERN.fullmatch(
        validator_version
    ):
        raise DataQualityConfigurationError(
            "Validator version must use semantic x.y.z form."
        )
    _validate_sha256(policy_fingerprint, "Policy fingerprint")
    payload = "|".join(
        (
            record_id,
            raw_content_sha256,
            dataset_kind.value,
            validator_version,
            policy_fingerprint,
        )
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def report_semantic_fingerprint(report: DataQualityReport) -> str:
    document = report_to_document(report)
    document.pop("report_id")
    document.pop("checked_at")
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


def report_to_document(report: DataQualityReport) -> dict[str, object]:
    return {
        "report_id": report.report_id,
        "assessment_key": report.assessment_key,
        "record_id": report.record_id,
        "raw_content_sha256": report.raw_content_sha256,
        "dataset_kind": report.dataset_kind.value,
        "status": report.status.value,
        "checked_at": _format_datetime(report.checked_at),
        "validator_version": report.validator_version,
        "policy_fingerprint": report.policy_fingerprint,
        "item_count": report.item_count,
        "issues": [issue_to_document(issue) for issue in report.issues],
    }


def report_from_document(document: object) -> DataQualityReport:
    if not isinstance(document, dict):
        raise DataQualityStoreIntegrityError(
            "Stored quality report is not a JSON object."
        )
    typed_document = cast(dict[str, object], document)
    try:
        raw_issues = typed_document["issues"]
        if not isinstance(raw_issues, list):
            raise TypeError("issues")
        return DataQualityReport(
            report_id=_required_string(typed_document, "report_id"),
            assessment_key=_required_string(typed_document, "assessment_key"),
            record_id=_required_string(typed_document, "record_id"),
            raw_content_sha256=_required_string(
                typed_document,
                "raw_content_sha256",
            ),
            dataset_kind=QualityDatasetKind(
                _required_string(typed_document, "dataset_kind")
            ),
            status=QualityStatus(_required_string(typed_document, "status")),
            checked_at=_parse_datetime(
                _required_string(typed_document, "checked_at")
            ),
            validator_version=_required_string(
                typed_document,
                "validator_version",
            ),
            policy_fingerprint=_required_string(
                typed_document,
                "policy_fingerprint",
            ),
            item_count=_required_int(typed_document, "item_count"),
            issues=tuple(issue_from_document(item) for item in raw_issues),
        )
    except (KeyError, TypeError, ValueError, DataQualityConfigurationError) as error:
        raise DataQualityStoreIntegrityError(
            "Stored quality report is invalid."
        ) from error


def issue_to_document(issue: QualityIssue) -> dict[str, object]:
    return {
        "code": issue.code.value,
        "severity": issue.severity.value,
        "message": issue.message,
        "item_index": issue.item_index,
        "timestamp": (
            _format_datetime(issue.timestamp)
            if issue.timestamp is not None
            else None
        ),
        "details": dict(issue.details),
    }


def issue_from_document(document: object) -> QualityIssue:
    if not isinstance(document, dict):
        raise DataQualityStoreIntegrityError(
            "Stored quality issue is not a JSON object."
        )
    typed_document = cast(dict[str, object], document)
    timestamp_value = typed_document.get("timestamp")
    if timestamp_value is not None and not isinstance(timestamp_value, str):
        raise DataQualityStoreIntegrityError(
            "Stored quality issue timestamp is invalid."
        )
    details = typed_document.get("details")
    if not isinstance(details, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in details.items()
    ):
        raise DataQualityStoreIntegrityError(
            "Stored quality issue details are invalid."
        )
    try:
        return QualityIssue(
            code=QualityIssueCode(_required_string(typed_document, "code")),
            severity=QualitySeverity(
                _required_string(typed_document, "severity")
            ),
            message=_required_string(typed_document, "message"),
            item_index=_optional_int(typed_document, "item_index"),
            timestamp=(
                _parse_datetime(timestamp_value)
                if timestamp_value is not None
                else None
            ),
            details=cast(dict[str, str], details),
        )
    except (ValueError, DataQualityConfigurationError) as error:
        raise DataQualityStoreIntegrityError(
            "Stored quality issue is invalid."
        ) from error


def canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise DataQualityStoreIntegrityError(
            "Quality report cannot be serialized as canonical JSON."
        ) from error


def _required_string(document: Mapping[str, object], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str):
        raise DataQualityStoreIntegrityError(
            f"Stored quality report field {key!r} is invalid."
        )
    return value


def _required_int(document: Mapping[str, object], key: str) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DataQualityStoreIntegrityError(
            f"Stored quality report field {key!r} is invalid."
        )
    return value


def _optional_int(document: Mapping[str, object], key: str) -> int | None:
    value = document.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise DataQualityStoreIntegrityError(
            f"Stored quality report field {key!r} is invalid."
        )
    return value


def _timedelta_microseconds(value: timedelta) -> int:
    return (
        value.days * 86_400_000_000
        + value.seconds * 1_000_000
        + value.microseconds
    )


def _require_nonnegative_timedelta(value: object, field_name: str) -> None:
    if not isinstance(value, timedelta) or value < timedelta(0):
        raise DataQualityConfigurationError(
            f"{field_name} must be a nonnegative timedelta."
        )


def _require_aware_datetime(value: object, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise DataQualityConfigurationError(
            f"{field_name} must include timezone information."
        )


def _validate_uuid(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise DataQualityConfigurationError(f"{field_name} must be a UUID string.")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise DataQualityConfigurationError(
            f"{field_name} must be a UUID string."
        ) from error
    if str(parsed) != value:
        raise DataQualityConfigurationError(
            f"{field_name} must use canonical UUID form."
        )


def _validate_sha256(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise DataQualityConfigurationError(
            f"{field_name} must be a lowercase SHA-256 hex digest."
        )


def _format_datetime(value: datetime) -> str:
    _require_aware_datetime(value, "Timestamp")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise DataQualityStoreIntegrityError(
            "Stored quality timestamp is not valid ISO-8601."
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DataQualityStoreIntegrityError(
            "Stored quality timestamp lacks timezone data."
        )
    return parsed.astimezone(UTC)
