from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from world_quant_system.data import (
    FileRawMarketDataStore,
    MarketDataNormalizer,
    MarketDataQualityGate,
    RawMarketDataCapture,
    SQLiteDataQualityStore,
    SQLiteNormalizedMarketDataStore,
)
from world_quant_system.data.quality_models import QualityStatus
from world_quant_system.domain import Candle, CandleInterval, CandlePage
from world_quant_system.research.historical_dataset_models import (
    HistoricalDatasetEligibilityError,
    HistoricalDatasetImportSpec,
    HistoricalDatasetIssue,
    HistoricalDatasetIssueCode,
    HistoricalDatasetManifest,
    HistoricalDatasetSnapshot,
    MissingSessionPolicy,
)
from world_quant_system.research.historical_dataset_store import (
    SQLiteHistoricalDatasetStore,
    _normalized_digest,
)

_EXPECTED_HEADERS = (
    "timestamp",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "currency",
)
_MAX_SOURCE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _FixedClock:
    value: datetime

    def now_utc(self) -> datetime:
        return self.value


@dataclass(frozen=True, slots=True)
class ParsedHistoricalCsv:
    candles: tuple[Candle, ...]
    issues: tuple[HistoricalDatasetIssue, ...]
    source_text: str
    source_sha256: str


class HistoricalDatasetImporter:
    """Import a strict networkless CSV through the existing data pipeline."""

    def __init__(self, store: SQLiteHistoricalDatasetStore) -> None:
        self._store = store

    async def import_csv(
        self,
        source_file: Path | str,
        spec: HistoricalDatasetImportSpec,
    ) -> HistoricalDatasetSnapshot:
        path = Path(source_file).expanduser().resolve()
        parsed = parse_historical_csv(path, spec)
        dataset_id = spec.dataset_id(parsed.source_sha256)
        dataset_root = self._store.dataset_root(dataset_id)
        dataset_root.mkdir(parents=True, exist_ok=True, mode=0o700)

        try:
            captured_at = parsed.candles[-1].timestamp.astimezone(UTC) + timedelta(
                seconds=1
            )
            normalized_at = captured_at + timedelta(microseconds=1)
        except OverflowError as error:
            raise HistoricalDatasetEligibilityError(
                "Historical CSV end timestamp is too large to import safely."
            ) from error

        raw_store = FileRawMarketDataStore(dataset_root / "raw")
        metadata = await raw_store.record(
            RawMarketDataCapture(
                provider=spec.provider,
                endpoint="/api/v1/candles",
                request_params={
                    "dataset_id": dataset_id,
                    "exchange": spec.exchange,
                    "symbol": spec.symbol,
                    "interval": spec.interval.value,
                    "timezone": spec.policy.timezone,
                    "source_format": spec.source_format.value,
                    "source_sha256": parsed.source_sha256,
                },
                captured_at=captured_at,
                status_code=200,
                response_headers={"Content-Type": "text/csv; charset=utf-8"},
                json_body={
                    "dataset_id": dataset_id,
                    "row_count": len(parsed.candles),
                    "source_sha256": parsed.source_sha256,
                },
                text=parsed.source_text,
                request_id=f"historical-dataset:{dataset_id}",
                idempotency_key=f"historical-dataset:{dataset_id}",
            )
        )

        quality_store = SQLiteDataQualityStore(dataset_root / "quality")
        quality_gate = MarketDataQualityGate(
            raw_store,
            quality_store,
            clock=_FixedClock(captured_at),
        )
        page = CandlePage(candles=parsed.candles, next_before=None)
        report = await quality_gate.assess_candle_page(metadata, page)
        if report.status is QualityStatus.QUARANTINE:
            raise HistoricalDatasetEligibilityError(
                "CSV data was quarantined by the market-data quality gate."
            )

        normalized_store = SQLiteNormalizedMarketDataStore(
            dataset_root / "normalized"
        )
        normalizer = MarketDataNormalizer(
            normalized_store,
            clock=_FixedClock(normalized_at),
        )
        records = await normalizer.normalize_candle_page(report, page)
        normalized_digest, normalized_count = await _normalized_digest(
            normalized_store,
            symbol=spec.symbol,
            interval=spec.interval,
        )
        if normalized_count != len(records) or normalized_count != len(parsed.candles):
            raise HistoricalDatasetEligibilityError(
                "Normalized item count does not match the imported source."
            )

        manifest = HistoricalDatasetManifest(
            dataset_id=dataset_id,
            provider=spec.provider,
            exchange=spec.exchange,
            symbols=(spec.symbol,),
            interval=spec.interval,
            timezone=spec.policy.timezone,
            currency=spec.currency,
            start=parsed.candles[0].timestamp.astimezone(UTC),
            end=parsed.candles[-1].timestamp.astimezone(UTC),
            item_count=len(parsed.candles),
            imported_at=normalized_at,
            source_format=spec.source_format,
            source_name=path.name,
            source_sha256=parsed.source_sha256,
            raw_record_id=metadata.record_id,
            raw_content_sha256=metadata.content_sha256,
            quality_report_id=report.report_id,
            quality_status=report.status,
            quality_policy_digest=report.policy_fingerprint,
            normalizer_version=normalizer.VERSION,
            normalized_digest=normalized_digest,
            point_in_time_context_digest=spec.point_in_time_context_digest,
            corporate_action_context_digest=(
                spec.corporate_action_context_digest
            ),
            code_commit=spec.code_commit,
            policy=spec.policy,
            issues=parsed.issues,
        )
        snapshot = await self._store.save_manifest(manifest)
        return await self._store.verify(snapshot.manifest.dataset_id)


def parse_historical_csv(
    source_file: Path | str,
    spec: HistoricalDatasetImportSpec,
) -> ParsedHistoricalCsv:
    path = Path(source_file).expanduser().resolve()
    try:
        source_bytes = path.read_bytes()
    except OSError as error:
        raise HistoricalDatasetEligibilityError(
            "Historical CSV file could not be read."
        ) from error
    if not source_bytes:
        raise HistoricalDatasetEligibilityError(
            "Historical CSV file cannot be empty."
        )
    if len(source_bytes) > _MAX_SOURCE_BYTES:
        raise HistoricalDatasetEligibilityError(
            "Historical CSV exceeds the 8 MiB import limit."
        )
    try:
        source_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise HistoricalDatasetEligibilityError(
            "Historical CSV must use strict UTF-8 encoding."
        ) from error
    if source_text.startswith("\ufeff"):
        raise HistoricalDatasetEligibilityError(
            "Historical CSV must not contain a UTF-8 byte-order mark."
        )

    reader = csv.DictReader(io.StringIO(source_text, newline=""))
    if reader.fieldnames is None or tuple(reader.fieldnames) != _EXPECTED_HEADERS:
        raise HistoricalDatasetEligibilityError(
            "Historical CSV header must be exactly: "
            + ",".join(_EXPECTED_HEADERS)
        )

    zone = ZoneInfo(spec.policy.timezone)
    candles: list[Candle] = []
    previous: datetime | None = None
    seen_timestamps: set[datetime] = set()
    for row_number, row in enumerate(reader, start=2):
        if None in row:
            raise HistoricalDatasetEligibilityError(
                f"CSV row {row_number} contains extra columns."
            )
        if any(value is None for value in row.values()):
            raise HistoricalDatasetEligibilityError(
                f"CSV row {row_number} contains missing columns."
            )
        timestamp = _parse_timestamp(row["timestamp"], zone, row_number)
        if previous is not None and timestamp <= previous:
            raise HistoricalDatasetEligibilityError(
                "Historical CSV timestamps must be strictly increasing."
            )
        if timestamp in seen_timestamps:
            raise HistoricalDatasetEligibilityError(
                "Historical CSV contains duplicate timestamps."
            )
        seen_timestamps.add(timestamp)
        previous = timestamp

        symbol = _required_value(row["symbol"], "symbol", row_number).upper()
        if symbol != spec.symbol:
            raise HistoricalDatasetEligibilityError(
                "Historical CSV cannot mix or substitute symbols."
            )
        currency = _required_value(row["currency"], "currency", row_number).upper()
        if currency != spec.currency:
            raise HistoricalDatasetEligibilityError(
                "Historical CSV currency does not match the import specification."
            )
        try:
            candle = Candle(
                symbol=symbol,
                interval=spec.interval,
                timestamp=timestamp,
                open_price=_decimal(row["open"], "open", row_number),
                high_price=_decimal(row["high"], "high", row_number),
                low_price=_decimal(row["low"], "low", row_number),
                close_price=_decimal(row["close"], "close", row_number),
                volume=_volume(row["volume"], row_number),
                currency=currency,
                source=spec.provider,
            )
        except ValueError as error:
            raise HistoricalDatasetEligibilityError(
                f"CSV row {row_number} violates candle invariants: {error}"
            ) from error
        candles.append(candle)

    if not candles:
        raise HistoricalDatasetEligibilityError(
            "Historical CSV must contain at least one candle."
        )
    issues = _session_issues(tuple(candles), spec)
    if issues and spec.policy.missing_session_policy is MissingSessionPolicy.REJECT:
        dates = ", ".join(
            issue.session_date.isoformat()
            for issue in issues
            if issue.session_date is not None
        )
        raise HistoricalDatasetEligibilityError(
            f"Historical CSV is missing expected sessions: {dates}."
        )
    return ParsedHistoricalCsv(
        candles=tuple(candles),
        issues=issues,
        source_text=source_text,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
    )


def _session_issues(
    candles: tuple[Candle, ...],
    spec: HistoricalDatasetImportSpec,
) -> tuple[HistoricalDatasetIssue, ...]:
    if spec.interval is not CandleInterval.DAY_1:
        return ()
    zone = ZoneInfo(spec.policy.timezone)
    actual_dates = {candle.timestamp.astimezone(zone).date() for candle in candles}
    first = min(actual_dates)
    last = max(actual_dates)
    holidays = set(spec.policy.holidays)
    expected_weekdays = set(spec.policy.expected_weekdays)
    missing: list[HistoricalDatasetIssue] = []
    cursor = first
    while cursor <= last:
        if (
            cursor.weekday() in expected_weekdays
            and cursor not in holidays
            and cursor not in actual_dates
        ):
            missing.append(
                HistoricalDatasetIssue(
                    code=HistoricalDatasetIssueCode.MISSING_SESSION,
                    message="Expected trading session is missing from the source.",
                    session_date=cursor,
                )
            )
        cursor += timedelta(days=1)
    return tuple(missing)


def _parse_timestamp(value: str | None, zone: ZoneInfo, row_number: int) -> datetime:
    raw = _required_value(value, "timestamp", row_number)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise HistoricalDatasetEligibilityError(
            f"CSV row {row_number} timestamp is not ISO-8601."
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HistoricalDatasetEligibilityError(
            f"CSV row {row_number} timestamp must include a UTC offset."
        )
    expected_offset = parsed.astimezone(zone).utcoffset()
    if parsed.utcoffset() != expected_offset:
        raise HistoricalDatasetEligibilityError(
            f"CSV row {row_number} timestamp offset does not match "
            f"{zone.key}."
        )
    return parsed.astimezone(UTC)


def _decimal(value: str | None, field_name: str, row_number: int) -> Decimal:
    raw = _required_value(value, field_name, row_number)
    try:
        parsed = Decimal(raw)
    except InvalidOperation as error:
        raise HistoricalDatasetEligibilityError(
            f"CSV row {row_number} {field_name} is not a decimal."
        ) from error
    if not parsed.is_finite():
        raise HistoricalDatasetEligibilityError(
            f"CSV row {row_number} {field_name} must be finite."
        )
    return parsed


def _volume(value: str | None, row_number: int) -> int:
    raw = _required_value(value, "volume", row_number)
    if not raw.isascii() or not raw.isdigit():
        raise HistoricalDatasetEligibilityError(
            f"CSV row {row_number} volume must be a nonnegative integer."
        )
    return int(raw)


def _required_value(value: str | None, field_name: str, row_number: int) -> str:
    if value is None or not value.strip():
        raise HistoricalDatasetEligibilityError(
            f"CSV row {row_number} {field_name} cannot be empty."
        )
    if value != value.strip():
        raise HistoricalDatasetEligibilityError(
            f"CSV row {row_number} {field_name} cannot contain outer whitespace."
        )
    return value


__all__ = [
    "HistoricalDatasetImporter",
    "ParsedHistoricalCsv",
    "parse_historical_csv",
]
