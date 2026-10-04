from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from world_quant_system.data.quality_models import (
    DataQualityReport,
    QualityDatasetKind,
    QualityStatus,
)
from world_quant_system.domain.models import Candle, CandleInterval, CandlePage, Quote

_SCHEMA_VERSION = 1
_NORMALIZER_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class NormalizedMarketDataError(Exception):
    """Base exception for normalized market-data failures."""


class NormalizedMarketDataConfigurationError(NormalizedMarketDataError):
    """Raised when normalized storage or replay configuration is invalid."""


class NormalizedMarketDataRejectedError(NormalizedMarketDataError):
    """Raised when quarantined data is submitted for normalization."""


class NormalizedMarketDataConflictError(NormalizedMarketDataError):
    """Raised when a natural key is reused for different market data."""


class NormalizedMarketDataIntegrityError(NormalizedMarketDataError):
    """Raised when normalized catalog contents fail integrity validation."""


class NormalizedMarketDataNotFoundError(NormalizedMarketDataError):
    """Raised when a normalized record does not exist."""


class NormalizedDataKind(StrEnum):
    QUOTE = "quote"
    CANDLE = "candle"


@dataclass(frozen=True, slots=True)
class NormalizationLineage:
    lineage_id: str
    item_kind: NormalizedDataKind
    item_id: str
    raw_record_id: str
    raw_content_sha256: str
    quality_report_id: str
    quality_status: QualityStatus
    normalized_at: datetime
    normalizer_version: str

    def __post_init__(self) -> None:
        _validate_uuid(self.lineage_id, "Lineage ID")
        if not isinstance(self.item_kind, NormalizedDataKind):
            raise NormalizedMarketDataConfigurationError(
                "Lineage item kind must be a NormalizedDataKind value."
            )
        _validate_uuid(self.item_id, "Item ID")
        _validate_uuid(self.raw_record_id, "Raw record ID")
        _validate_sha256(self.raw_content_sha256, "Raw content SHA-256")
        _validate_uuid(self.quality_report_id, "Quality report ID")
        _validate_accepted_quality_status(self.quality_status)
        _require_aware_datetime(self.normalized_at, "Normalized-at timestamp")
        _validate_normalizer_version(self.normalizer_version)


@dataclass(frozen=True, slots=True)
class NormalizedQuoteRecord:
    item_id: str
    quote: Quote
    quality_status: QualityStatus
    raw_record_id: str
    raw_content_sha256: str
    quality_report_id: str
    normalized_at: datetime
    normalizer_version: str
    content_sha256: str
    schema_version: int
    lineage_count: int

    def __post_init__(self) -> None:
        _validate_uuid(self.item_id, "Item ID")
        if not isinstance(self.quote, Quote):
            raise NormalizedMarketDataConfigurationError(
                "Normalized quote record must contain a Quote."
            )
        _validate_common_record_fields(
            quality_status=self.quality_status,
            raw_record_id=self.raw_record_id,
            raw_content_sha256=self.raw_content_sha256,
            quality_report_id=self.quality_report_id,
            normalized_at=self.normalized_at,
            normalizer_version=self.normalizer_version,
            content_sha256=self.content_sha256,
            schema_version=self.schema_version,
            lineage_count=self.lineage_count,
        )


@dataclass(frozen=True, slots=True)
class NormalizedCandleRecord:
    item_id: str
    candle: Candle
    quality_status: QualityStatus
    raw_record_id: str
    raw_content_sha256: str
    quality_report_id: str
    normalized_at: datetime
    normalizer_version: str
    content_sha256: str
    schema_version: int
    lineage_count: int

    def __post_init__(self) -> None:
        _validate_uuid(self.item_id, "Item ID")
        if not isinstance(self.candle, Candle):
            raise NormalizedMarketDataConfigurationError(
                "Normalized candle record must contain a Candle."
            )
        _validate_common_record_fields(
            quality_status=self.quality_status,
            raw_record_id=self.raw_record_id,
            raw_content_sha256=self.raw_content_sha256,
            quality_report_id=self.quality_report_id,
            normalized_at=self.normalized_at,
            normalizer_version=self.normalizer_version,
            content_sha256=self.content_sha256,
            schema_version=self.schema_version,
            lineage_count=self.lineage_count,
        )


@dataclass(frozen=True, slots=True)
class NormalizedCandleCursor:
    timestamp: datetime
    symbol: str
    item_id: str

    def __post_init__(self) -> None:
        _require_aware_datetime(self.timestamp, "Cursor timestamp")
        _require_nonblank(self.symbol, "Cursor symbol")
        _validate_uuid(self.item_id, "Cursor item ID")


class NormalizedMarketDataWriter(Protocol):
    async def save_quotes(
        self,
        quotes: Sequence[Quote],
        report: DataQualityReport,
        *,
        normalized_at: datetime,
        normalizer_version: str,
    ) -> tuple[NormalizedQuoteRecord, ...]:
        """Persist one quality-approved quote dataset."""
        ...

    async def save_candle_page(
        self,
        page: CandlePage,
        report: DataQualityReport,
        *,
        normalized_at: datetime,
        normalizer_version: str,
    ) -> tuple[NormalizedCandleRecord, ...]:
        """Persist one quality-approved candle page."""
        ...


class NormalizedMarketDataReader(Protocol):
    async def query_candles(
        self,
        *,
        symbols: Sequence[str] | None = None,
        interval: CandleInterval | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        statuses: Sequence[QualityStatus] | None = None,
        after: NormalizedCandleCursor | None = None,
        limit: int = 1_000,
    ) -> tuple[NormalizedCandleRecord, ...]:
        """Return normalized candles in deterministic event order."""
        ...


def validate_normalization_request(
    report: DataQualityReport,
    *,
    expected_kind: QualityDatasetKind,
    item_count: int,
    normalized_at: datetime,
    normalizer_version: str,
) -> None:
    if not isinstance(report, DataQualityReport):
        raise NormalizedMarketDataConfigurationError(
            "Normalization requires a DataQualityReport."
        )
    if report.status is QualityStatus.QUARANTINE:
        raise NormalizedMarketDataRejectedError(
            "Quarantined market data cannot enter normalized storage."
        )
    if report.dataset_kind is not expected_kind:
        raise NormalizedMarketDataConfigurationError(
            "Quality-report dataset kind does not match normalized data."
        )
    if report.item_count != item_count:
        raise NormalizedMarketDataConfigurationError(
            "Quality-report item count does not match normalized data."
        )
    _require_aware_datetime(normalized_at, "Normalized-at timestamp")
    _validate_normalizer_version(normalizer_version)


def quote_document(quote: Quote) -> dict[str, object]:
    return {
        "kind": NormalizedDataKind.QUOTE.value,
        "source": quote.source,
        "symbol": quote.symbol,
        "timestamp": format_utc(quote.timestamp),
        "price": format(quote.price, "f"),
        "currency": quote.currency,
        "schema_version": _SCHEMA_VERSION,
    }


def candle_document(candle: Candle) -> dict[str, object]:
    return {
        "kind": NormalizedDataKind.CANDLE.value,
        "source": candle.source,
        "symbol": candle.symbol,
        "interval": candle.interval.value,
        "timestamp": format_utc(candle.timestamp),
        "open_price": format(candle.open_price, "f"),
        "high_price": format(candle.high_price, "f"),
        "low_price": format(candle.low_price, "f"),
        "close_price": format(candle.close_price, "f"),
        "volume": candle.volume,
        "currency": candle.currency,
        "schema_version": _SCHEMA_VERSION,
    }


def quote_from_document(document: Mapping[str, object]) -> Quote:
    _require_schema(document, NormalizedDataKind.QUOTE)
    return Quote(
        symbol=_required_string(document, "symbol"),
        price=_required_decimal(document, "price"),
        timestamp=parse_utc(_required_string(document, "timestamp")),
        currency=_required_string(document, "currency"),
        source=_required_string(document, "source"),
    )


def candle_from_document(document: Mapping[str, object]) -> Candle:
    _require_schema(document, NormalizedDataKind.CANDLE)
    return Candle(
        symbol=_required_string(document, "symbol"),
        interval=CandleInterval(_required_string(document, "interval")),
        timestamp=parse_utc(_required_string(document, "timestamp")),
        open_price=_required_decimal(document, "open_price"),
        high_price=_required_decimal(document, "high_price"),
        low_price=_required_decimal(document, "low_price"),
        close_price=_required_decimal(document, "close_price"),
        volume=_required_int(document, "volume"),
        currency=_required_string(document, "currency"),
        source=_required_string(document, "source"),
    )


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
        raise NormalizedMarketDataIntegrityError(
            "Normalized market data cannot be serialized canonically."
        ) from error


def content_sha256(document: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


def format_utc(value: datetime) -> str:
    _require_aware_datetime(value, "Timestamp")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise NormalizedMarketDataIntegrityError(
            "Stored normalized timestamp is invalid."
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise NormalizedMarketDataIntegrityError(
            "Stored normalized timestamp lacks timezone information."
        )
    return parsed.astimezone(UTC)


def _validate_common_record_fields(
    *,
    quality_status: QualityStatus,
    raw_record_id: str,
    raw_content_sha256: str,
    quality_report_id: str,
    normalized_at: datetime,
    normalizer_version: str,
    content_sha256: str,
    schema_version: int,
    lineage_count: int,
) -> None:
    _validate_accepted_quality_status(quality_status)
    _validate_uuid(raw_record_id, "Raw record ID")
    _validate_sha256(raw_content_sha256, "Raw content SHA-256")
    _validate_uuid(quality_report_id, "Quality report ID")
    _require_aware_datetime(normalized_at, "Normalized-at timestamp")
    _validate_normalizer_version(normalizer_version)
    _validate_sha256(content_sha256, "Content SHA-256")
    if schema_version != _SCHEMA_VERSION:
        raise NormalizedMarketDataIntegrityError(
            f"Unsupported normalized schema version: {schema_version}."
        )
    if (
        isinstance(lineage_count, bool)
        or not isinstance(lineage_count, int)
        or lineage_count < 1
    ):
        raise NormalizedMarketDataIntegrityError(
            "Lineage count must be a positive integer."
        )


def _validate_accepted_quality_status(status: object) -> None:
    if status not in (QualityStatus.PASS, QualityStatus.WARNING):
        raise NormalizedMarketDataRejectedError(
            "Normalized storage accepts only PASS or WARNING data."
        )


def _validate_normalizer_version(value: object) -> None:
    if not isinstance(value, str) or not _NORMALIZER_VERSION_PATTERN.fullmatch(value):
        raise NormalizedMarketDataConfigurationError(
            "Normalizer version must use semantic x.y.z form."
        )


def _validate_uuid(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise NormalizedMarketDataConfigurationError(
            f"{field_name} must be a UUID string."
        )
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise NormalizedMarketDataConfigurationError(
            f"{field_name} must be a UUID string."
        ) from error
    if str(parsed) != value:
        raise NormalizedMarketDataConfigurationError(
            f"{field_name} must use canonical UUID form."
        )


def _validate_sha256(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise NormalizedMarketDataConfigurationError(
            f"{field_name} must be a lowercase SHA-256 digest."
        )


def _require_aware_datetime(value: object, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise NormalizedMarketDataConfigurationError(
            f"{field_name} must include timezone information."
        )


def _require_nonblank(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise NormalizedMarketDataConfigurationError(
            f"{field_name} must be a nonblank string."
        )


def _require_schema(
    document: Mapping[str, object],
    expected_kind: NormalizedDataKind,
) -> None:
    if _required_string(document, "kind") != expected_kind.value:
        raise NormalizedMarketDataIntegrityError(
            "Stored normalized record kind is invalid."
        )
    if _required_int(document, "schema_version") != _SCHEMA_VERSION:
        raise NormalizedMarketDataIntegrityError(
            "Stored normalized schema version is unsupported."
        )


def _required_string(document: Mapping[str, object], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str):
        raise NormalizedMarketDataIntegrityError(
            f"Stored normalized field {key!r} is invalid."
        )
    return value


def _required_int(document: Mapping[str, object], key: str) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise NormalizedMarketDataIntegrityError(
            f"Stored normalized field {key!r} is invalid."
        )
    return value


def _required_decimal(document: Mapping[str, object], key: str) -> Decimal:
    value = _required_string(document, key)
    try:
        parsed = Decimal(value)
    except ArithmeticError as error:
        raise NormalizedMarketDataIntegrityError(
            f"Stored normalized field {key!r} is invalid."
        ) from error
    if not parsed.is_finite():
        raise NormalizedMarketDataIntegrityError(
            f"Stored normalized field {key!r} is non-finite."
        )
    return parsed
