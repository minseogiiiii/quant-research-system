from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid5

from world_quant_system.data.normalized_models import (
    NormalizationLineage,
    NormalizedCandleCursor,
    NormalizedCandleRecord,
    NormalizedDataKind,
    NormalizedMarketDataConfigurationError,
    NormalizedMarketDataConflictError,
    NormalizedMarketDataIntegrityError,
    NormalizedMarketDataNotFoundError,
    NormalizedQuoteRecord,
    candle_document,
    candle_from_document,
    canonical_json_bytes,
    format_utc,
    parse_utc,
    quote_document,
    quote_from_document,
    validate_normalization_request,
)
from world_quant_system.data.quality_models import (
    DataQualityReport,
    QualityDatasetKind,
    QualityStatus,
)
from world_quant_system.domain.models import CandleInterval, CandlePage, Quote

_SCHEMA_VERSION = 1
_MAX_BATCH_ITEMS = 100_000
_ITEM_NAMESPACE = UUID("d179ea87-59cb-53f2-b734-836a4803b5aa")
_LINEAGE_NAMESPACE = UUID("16f48ae9-bc40-541f-bd5a-3e2b3270ac9e")

_QUOTE_COLUMNS = (
    "item_id, source, symbol, event_time, price, currency, quality_status, "
    "first_raw_record_id, first_raw_content_sha256, first_quality_report_id, "
    "normalized_at, normalizer_version, content_json, content_sha256, "
    "schema_version, lineage_count"
)
_CANDLE_COLUMNS = (
    "item_id, source, symbol, interval, event_time, open_price, high_price, "
    "low_price, close_price, volume, currency, quality_status, "
    "first_raw_record_id, first_raw_content_sha256, first_quality_report_id, "
    "normalized_at, normalizer_version, content_json, content_sha256, "
    "schema_version, lineage_count"
)
_LINEAGE_COLUMNS = (
    "lineage_id, item_kind, item_id, raw_record_id, raw_content_sha256, "
    "quality_report_id, quality_status, normalized_at, normalizer_version"
)


class SQLiteNormalizedMarketDataStore:
    """Append-only normalized storage with deterministic keys and lineage."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._catalog_path = self._root / "normalized.sqlite3"
        self._initialize()

    @property
    def root(self) -> Path:
        return self._root

    async def save_quotes(
        self,
        quotes: Sequence[Quote],
        report: DataQualityReport,
        *,
        normalized_at: datetime,
        normalizer_version: str,
    ) -> tuple[NormalizedQuoteRecord, ...]:
        quote_tuple = tuple(quotes)
        validate_normalization_request(
            report,
            expected_kind=QualityDatasetKind.QUOTES,
            item_count=len(quote_tuple),
            normalized_at=normalized_at,
            normalizer_version=normalizer_version,
        )
        self._validate_batch_size(len(quote_tuple))
        return await asyncio.to_thread(
            self._save_quotes_sync,
            quote_tuple,
            report,
            normalized_at.astimezone(UTC),
            normalizer_version,
        )

    async def save_candle_page(
        self,
        page: CandlePage,
        report: DataQualityReport,
        *,
        normalized_at: datetime,
        normalizer_version: str,
    ) -> tuple[NormalizedCandleRecord, ...]:
        if not isinstance(page, CandlePage):
            raise NormalizedMarketDataConfigurationError(
                "Normalized candle input must be a CandlePage."
            )
        validate_normalization_request(
            report,
            expected_kind=QualityDatasetKind.CANDLES,
            item_count=len(page.candles),
            normalized_at=normalized_at,
            normalizer_version=normalizer_version,
        )
        self._validate_batch_size(len(page.candles))
        return await asyncio.to_thread(
            self._save_candles_sync,
            page,
            report,
            normalized_at.astimezone(UTC),
            normalizer_version,
        )

    async def get_quote(self, item_id: str) -> NormalizedQuoteRecord:
        return await asyncio.to_thread(self._get_quote_sync, item_id)

    async def get_candle(self, item_id: str) -> NormalizedCandleRecord:
        return await asyncio.to_thread(self._get_candle_sync, item_id)

    async def get_lineages(
        self,
        item_kind: NormalizedDataKind,
        item_id: str,
    ) -> tuple[NormalizationLineage, ...]:
        return await asyncio.to_thread(
            self._get_lineages_sync,
            item_kind,
            item_id,
        )

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
        normalized_symbols = self._normalize_symbol_filter(symbols)
        normalized_statuses = self._normalize_status_filter(statuses)
        self._validate_query_datetime(start, "Start timestamp")
        self._validate_query_datetime(end, "End timestamp")
        if start is not None and end is not None and start > end:
            raise NormalizedMarketDataConfigurationError(
                "Start timestamp cannot be later than end timestamp."
            )
        if interval is not None and not isinstance(interval, CandleInterval):
            raise NormalizedMarketDataConfigurationError(
                "Interval filter must be a CandleInterval value or None."
            )
        if after is not None and not isinstance(after, NormalizedCandleCursor):
            raise NormalizedMarketDataConfigurationError(
                "After cursor must be a NormalizedCandleCursor or None."
            )
        self._validate_limit(limit)
        return await asyncio.to_thread(
            self._query_candles_sync,
            normalized_symbols,
            interval,
            start.astimezone(UTC) if start is not None else None,
            end.astimezone(UTC) if end is not None else None,
            normalized_statuses,
            after,
            limit,
        )

    async def latest_quote(
        self,
        symbol: str,
        *,
        source: str | None = None,
        as_of: datetime | None = None,
    ) -> NormalizedQuoteRecord:
        normalized_symbol = self._normalize_symbol(symbol)
        if source is not None and (not isinstance(source, str) or not source.strip()):
            raise NormalizedMarketDataConfigurationError(
                "Source filter must be a nonblank string or None."
            )
        self._validate_query_datetime(as_of, "As-of timestamp")
        return await asyncio.to_thread(
            self._latest_quote_sync,
            normalized_symbol,
            source,
            as_of.astimezone(UTC) if as_of is not None else None,
        )

    def _save_quotes_sync(
        self,
        quotes: tuple[Quote, ...],
        report: DataQualityReport,
        normalized_at: datetime,
        normalizer_version: str,
    ) -> tuple[NormalizedQuoteRecord, ...]:
        self._reject_duplicate_quote_keys(quotes)
        item_ids: list[str] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for quote in quotes:
                document = quote_document(quote)
                item_id = self._deterministic_item_id(
                    NormalizedDataKind.QUOTE,
                    quote.source,
                    quote.symbol,
                    format_utc(quote.timestamp),
                )
                item_ids.append(item_id)
                self._upsert_quote(
                    connection,
                    item_id=item_id,
                    quote=quote,
                    document=document,
                    report=report,
                    normalized_at=normalized_at,
                    normalizer_version=normalizer_version,
                )
            return self._quote_records_by_ids(connection, item_ids)

    def _save_candles_sync(
        self,
        page: CandlePage,
        report: DataQualityReport,
        normalized_at: datetime,
        normalizer_version: str,
    ) -> tuple[NormalizedCandleRecord, ...]:
        item_ids: list[str] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for candle in page.candles:
                document = candle_document(candle)
                item_id = self._deterministic_item_id(
                    NormalizedDataKind.CANDLE,
                    candle.source,
                    candle.symbol,
                    candle.interval.value,
                    format_utc(candle.timestamp),
                )
                item_ids.append(item_id)
                self._upsert_candle(
                    connection,
                    item_id=item_id,
                    document=document,
                    report=report,
                    normalized_at=normalized_at,
                    normalizer_version=normalizer_version,
                )
            return self._candle_records_by_ids(connection, item_ids)


    def _quote_records_by_ids(
        self,
        connection: sqlite3.Connection,
        item_ids: list[str],
    ) -> tuple[NormalizedQuoteRecord, ...]:
        if not item_ids:
            return ()
        records: dict[str, NormalizedQuoteRecord] = {}
        for offset in range(0, len(item_ids), 900):
            chunk = item_ids[offset : offset + 900]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"SELECT {_QUOTE_COLUMNS} FROM normalized_quotes "
                f"WHERE item_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                record = self._quote_record_from_row(row)
                records[record.item_id] = record
        if len(records) != len(set(item_ids)):
            raise NormalizedMarketDataIntegrityError(
                "Normalized quote batch could not be read back completely."
            )
        return tuple(records[item_id] for item_id in item_ids)

    def _candle_records_by_ids(
        self,
        connection: sqlite3.Connection,
        item_ids: list[str],
    ) -> tuple[NormalizedCandleRecord, ...]:
        if not item_ids:
            return ()
        records: dict[str, NormalizedCandleRecord] = {}
        for offset in range(0, len(item_ids), 900):
            chunk = item_ids[offset : offset + 900]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"SELECT {_CANDLE_COLUMNS} FROM normalized_candles "
                f"WHERE item_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                record = self._candle_record_from_row(row)
                records[record.item_id] = record
        if len(records) != len(set(item_ids)):
            raise NormalizedMarketDataIntegrityError(
                "Normalized candle batch could not be read back completely."
            )
        return tuple(records[item_id] for item_id in item_ids)

    def _upsert_quote(
        self,
        connection: sqlite3.Connection,
        *,
        item_id: str,
        quote: Quote,
        document: dict[str, object],
        report: DataQualityReport,
        normalized_at: datetime,
        normalizer_version: str,
    ) -> None:
        serialized = canonical_json_bytes(document)
        digest = hashlib.sha256(serialized).hexdigest()
        row = connection.execute(
            "SELECT content_sha256 FROM normalized_quotes WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO normalized_quotes(
                    item_id, source, symbol, event_time, price, currency,
                    quality_status, first_raw_record_id,
                    first_raw_content_sha256, first_quality_report_id,
                    normalized_at, normalizer_version, content_json,
                    content_sha256, schema_version, lineage_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    item_id,
                    quote.source,
                    quote.symbol,
                    format_utc(quote.timestamp),
                    format(quote.price, "f"),
                    quote.currency,
                    report.status.value,
                    report.record_id,
                    report.raw_content_sha256,
                    report.report_id,
                    format_utc(normalized_at),
                    normalizer_version,
                    serialized.decode("utf-8"),
                    digest,
                    _SCHEMA_VERSION,
                ),
            )
        elif row[0] != digest:
            raise NormalizedMarketDataConflictError(
                "Quote natural key already exists with different content."
            )
        self._insert_lineage(
            connection,
            item_kind=NormalizedDataKind.QUOTE,
            item_id=item_id,
            report=report,
            normalized_at=normalized_at,
            normalizer_version=normalizer_version,
        )
        self._refresh_lineage_summary(connection, NormalizedDataKind.QUOTE, item_id)

    def _upsert_candle(
        self,
        connection: sqlite3.Connection,
        *,
        item_id: str,
        document: dict[str, object],
        report: DataQualityReport,
        normalized_at: datetime,
        normalizer_version: str,
    ) -> None:
        candle = candle_from_document(document)
        serialized = canonical_json_bytes(document)
        digest = hashlib.sha256(serialized).hexdigest()
        row = connection.execute(
            "SELECT content_sha256 FROM normalized_candles WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO normalized_candles(
                    item_id, source, symbol, interval, event_time, open_price,
                    high_price, low_price, close_price, volume, currency,
                    quality_status, first_raw_record_id,
                    first_raw_content_sha256, first_quality_report_id,
                    normalized_at, normalizer_version, content_json,
                    content_sha256, schema_version, lineage_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    item_id,
                    candle.source,
                    candle.symbol,
                    candle.interval.value,
                    format_utc(candle.timestamp),
                    format(candle.open_price, "f"),
                    format(candle.high_price, "f"),
                    format(candle.low_price, "f"),
                    format(candle.close_price, "f"),
                    candle.volume,
                    candle.currency,
                    report.status.value,
                    report.record_id,
                    report.raw_content_sha256,
                    report.report_id,
                    format_utc(normalized_at),
                    normalizer_version,
                    serialized.decode("utf-8"),
                    digest,
                    _SCHEMA_VERSION,
                ),
            )
        elif row[0] != digest:
            raise NormalizedMarketDataConflictError(
                "Candle natural key already exists with different content."
            )
        self._insert_lineage(
            connection,
            item_kind=NormalizedDataKind.CANDLE,
            item_id=item_id,
            report=report,
            normalized_at=normalized_at,
            normalizer_version=normalizer_version,
        )
        self._refresh_lineage_summary(connection, NormalizedDataKind.CANDLE, item_id)

    def _insert_lineage(
        self,
        connection: sqlite3.Connection,
        *,
        item_kind: NormalizedDataKind,
        item_id: str,
        report: DataQualityReport,
        normalized_at: datetime,
        normalizer_version: str,
    ) -> None:
        lineage_id = self._deterministic_lineage_id(
            item_kind,
            item_id,
            report.report_id,
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO normalization_lineage(
                lineage_id, item_kind, item_id, raw_record_id,
                raw_content_sha256, quality_report_id, quality_status,
                normalized_at, normalizer_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lineage_id,
                item_kind.value,
                item_id,
                report.record_id,
                report.raw_content_sha256,
                report.report_id,
                report.status.value,
                format_utc(normalized_at),
                normalizer_version,
            ),
        )

    def _refresh_lineage_summary(
        self,
        connection: sqlite3.Connection,
        item_kind: NormalizedDataKind,
        item_id: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT COUNT(*),
                   MAX(CASE WHEN quality_status = 'WARNING' THEN 1 ELSE 0 END)
            FROM normalization_lineage
            WHERE item_kind = ? AND item_id = ?
            """,
            (item_kind.value, item_id),
        ).fetchone()
        if row is None or not isinstance(row[0], int) or row[0] < 1:
            raise NormalizedMarketDataIntegrityError(
                "Normalized lineage summary could not be computed."
            )
        status = QualityStatus.WARNING if row[1] == 1 else QualityStatus.PASS
        table = (
            "normalized_quotes"
            if item_kind is NormalizedDataKind.QUOTE
            else "normalized_candles"
        )
        connection.execute(
            f"UPDATE {table} SET quality_status = ?, lineage_count = ? "
            "WHERE item_id = ?",
            (status.value, row[0], item_id),
        )

    def _get_quote_sync(self, item_id: str) -> NormalizedQuoteRecord:
        self._validate_item_id(item_id)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_QUOTE_COLUMNS} FROM normalized_quotes WHERE item_id = ?",
                (item_id,),
            ).fetchone()
        if row is None:
            raise NormalizedMarketDataNotFoundError(
                "Normalized quote record does not exist."
            )
        return self._quote_record_from_row(row)

    def _get_candle_sync(self, item_id: str) -> NormalizedCandleRecord:
        self._validate_item_id(item_id)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_CANDLE_COLUMNS} FROM normalized_candles WHERE item_id = ?",
                (item_id,),
            ).fetchone()
        if row is None:
            raise NormalizedMarketDataNotFoundError(
                "Normalized candle record does not exist."
            )
        return self._candle_record_from_row(row)

    def _get_lineages_sync(
        self,
        item_kind: NormalizedDataKind,
        item_id: str,
    ) -> tuple[NormalizationLineage, ...]:
        if not isinstance(item_kind, NormalizedDataKind):
            raise NormalizedMarketDataConfigurationError(
                "Lineage item kind must be a NormalizedDataKind value."
            )
        self._validate_item_id(item_id)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_LINEAGE_COLUMNS} FROM normalization_lineage "
                "WHERE item_kind = ? AND item_id = ? "
                "ORDER BY normalized_at ASC, lineage_id ASC",
                (item_kind.value, item_id),
            ).fetchall()
        if not rows:
            raise NormalizedMarketDataNotFoundError(
                "Normalized lineage does not exist."
            )
        return tuple(self._lineage_from_row(row) for row in rows)

    def _query_candles_sync(
        self,
        symbols: tuple[str, ...] | None,
        interval: CandleInterval | None,
        start: datetime | None,
        end: datetime | None,
        statuses: tuple[QualityStatus, ...] | None,
        after: NormalizedCandleCursor | None,
        limit: int,
    ) -> tuple[NormalizedCandleRecord, ...]:
        clauses: list[str] = []
        values: list[object] = []
        if symbols:
            placeholders = ",".join("?" for _ in symbols)
            clauses.append(f"symbol IN ({placeholders})")
            values.extend(symbols)
        if interval is not None:
            clauses.append("interval = ?")
            values.append(interval.value)
        if start is not None:
            clauses.append("event_time >= ?")
            values.append(format_utc(start))
        if end is not None:
            clauses.append("event_time <= ?")
            values.append(format_utc(end))
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"quality_status IN ({placeholders})")
            values.extend(status.value for status in statuses)
        if after is not None:
            cursor_time = format_utc(after.timestamp)
            clauses.append(
                "(event_time > ? OR "
                "(event_time = ? AND symbol > ?) OR "
                "(event_time = ? AND symbol = ? AND item_id > ?))"
            )
            values.extend(
                (
                    cursor_time,
                    cursor_time,
                    after.symbol,
                    cursor_time,
                    after.symbol,
                    after.item_id,
                )
            )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_CANDLE_COLUMNS} FROM normalized_candles {where} "
                "ORDER BY event_time ASC, symbol ASC, item_id ASC LIMIT ?",
                values,
            ).fetchall()
        return tuple(self._candle_record_from_row(row) for row in rows)

    def _latest_quote_sync(
        self,
        symbol: str,
        source: str | None,
        as_of: datetime | None,
    ) -> NormalizedQuoteRecord:
        clauses = ["symbol = ?"]
        values: list[object] = [symbol]
        if source is not None:
            clauses.append("source = ?")
            values.append(source)
        if as_of is not None:
            clauses.append("event_time <= ?")
            values.append(format_utc(as_of))
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_QUOTE_COLUMNS} FROM normalized_quotes "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY event_time DESC, item_id DESC LIMIT 1",
                values,
            ).fetchone()
        if row is None:
            raise NormalizedMarketDataNotFoundError(
                "No normalized quote matched the query."
            )
        return self._quote_record_from_row(row)

    def _quote_record_from_row(self, row: Sequence[object]) -> NormalizedQuoteRecord:
        document = self._verified_document(row[12], row[13])
        quote = quote_from_document(document)
        expected_catalog = (
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[14],
        )
        actual_catalog = (
            quote.source,
            quote.symbol,
            format_utc(quote.timestamp),
            format(quote.price, "f"),
            quote.currency,
            _SCHEMA_VERSION,
        )
        if expected_catalog != actual_catalog:
            raise NormalizedMarketDataIntegrityError(
                "Normalized quote document does not match catalog columns."
            )
        return NormalizedQuoteRecord(
            item_id=self._required_str(row[0], "item ID"),
            quote=quote,
            quality_status=QualityStatus(
                self._required_str(row[6], "quality status")
            ),
            raw_record_id=self._required_str(row[7], "raw record ID"),
            raw_content_sha256=self._required_str(row[8], "raw content SHA-256"),
            quality_report_id=self._required_str(row[9], "quality report ID"),
            normalized_at=parse_utc(
                self._required_str(row[10], "normalized-at timestamp")
            ),
            normalizer_version=self._required_str(row[11], "normalizer version"),
            content_sha256=self._required_str(row[13], "content SHA-256"),
            schema_version=self._required_int(row[14], "schema version"),
            lineage_count=self._required_int(row[15], "lineage count"),
        )

    def _candle_record_from_row(self, row: Sequence[object]) -> NormalizedCandleRecord:
        document = self._verified_document(row[17], row[18])
        candle = candle_from_document(document)
        expected_catalog = (
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
            row[8],
            row[9],
            row[10],
            row[19],
        )
        actual_catalog = (
            candle.source,
            candle.symbol,
            candle.interval.value,
            format_utc(candle.timestamp),
            format(candle.open_price, "f"),
            format(candle.high_price, "f"),
            format(candle.low_price, "f"),
            format(candle.close_price, "f"),
            candle.volume,
            candle.currency,
            _SCHEMA_VERSION,
        )
        if expected_catalog != actual_catalog:
            raise NormalizedMarketDataIntegrityError(
                "Normalized candle document does not match catalog columns."
            )
        return NormalizedCandleRecord(
            item_id=self._required_str(row[0], "item ID"),
            candle=candle,
            quality_status=QualityStatus(
                self._required_str(row[11], "quality status")
            ),
            raw_record_id=self._required_str(row[12], "raw record ID"),
            raw_content_sha256=self._required_str(row[13], "raw content SHA-256"),
            quality_report_id=self._required_str(row[14], "quality report ID"),
            normalized_at=parse_utc(
                self._required_str(row[15], "normalized-at timestamp")
            ),
            normalizer_version=self._required_str(row[16], "normalizer version"),
            content_sha256=self._required_str(row[18], "content SHA-256"),
            schema_version=self._required_int(row[19], "schema version"),
            lineage_count=self._required_int(row[20], "lineage count"),
        )

    def _lineage_from_row(self, row: Sequence[object]) -> NormalizationLineage:
        return NormalizationLineage(
            lineage_id=self._required_str(row[0], "lineage ID"),
            item_kind=NormalizedDataKind(
                self._required_str(row[1], "item kind")
            ),
            item_id=self._required_str(row[2], "item ID"),
            raw_record_id=self._required_str(row[3], "raw record ID"),
            raw_content_sha256=self._required_str(row[4], "raw content SHA-256"),
            quality_report_id=self._required_str(row[5], "quality report ID"),
            quality_status=QualityStatus(
                self._required_str(row[6], "quality status")
            ),
            normalized_at=parse_utc(
                self._required_str(row[7], "normalized-at timestamp")
            ),
            normalizer_version=self._required_str(row[8], "normalizer version"),
        )

    @staticmethod
    def _verified_document(
        raw_json: object,
        expected_sha256: object,
    ) -> dict[str, object]:
        if not isinstance(raw_json, str) or not isinstance(expected_sha256, str):
            raise NormalizedMarketDataIntegrityError(
                "Normalized catalog document fields are invalid."
            )
        serialized = raw_json.encode("utf-8")
        if hashlib.sha256(serialized).hexdigest() != expected_sha256:
            raise NormalizedMarketDataIntegrityError(
                "Normalized record failed SHA-256 verification."
            )
        try:
            parsed = json.loads(serialized)
        except json.JSONDecodeError as error:
            raise NormalizedMarketDataIntegrityError(
                "Normalized record is not valid JSON."
            ) from error
        if not isinstance(parsed, dict):
            raise NormalizedMarketDataIntegrityError(
                "Normalized record must be a JSON object."
            )
        return cast(dict[str, object], parsed)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS normalized_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS normalized_quotes (
                    item_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    price TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    quality_status TEXT NOT NULL CHECK (
                        quality_status IN ('PASS', 'WARNING')
                    ),
                    first_raw_record_id TEXT NOT NULL,
                    first_raw_content_sha256 TEXT NOT NULL,
                    first_quality_report_id TEXT NOT NULL,
                    normalized_at TEXT NOT NULL,
                    normalizer_version TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    lineage_count INTEGER NOT NULL,
                    UNIQUE(source, symbol, event_time)
                );

                CREATE TABLE IF NOT EXISTS normalized_candles (
                    item_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    open_price TEXT NOT NULL,
                    high_price TEXT NOT NULL,
                    low_price TEXT NOT NULL,
                    close_price TEXT NOT NULL,
                    volume INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    quality_status TEXT NOT NULL CHECK (
                        quality_status IN ('PASS', 'WARNING')
                    ),
                    first_raw_record_id TEXT NOT NULL,
                    first_raw_content_sha256 TEXT NOT NULL,
                    first_quality_report_id TEXT NOT NULL,
                    normalized_at TEXT NOT NULL,
                    normalizer_version TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    lineage_count INTEGER NOT NULL,
                    UNIQUE(source, symbol, interval, event_time)
                );

                CREATE TABLE IF NOT EXISTS normalization_lineage (
                    lineage_id TEXT PRIMARY KEY,
                    item_kind TEXT NOT NULL CHECK (
                        item_kind IN ('quote', 'candle')
                    ),
                    item_id TEXT NOT NULL,
                    raw_record_id TEXT NOT NULL,
                    raw_content_sha256 TEXT NOT NULL,
                    quality_report_id TEXT NOT NULL,
                    quality_status TEXT NOT NULL CHECK (
                        quality_status IN ('PASS', 'WARNING')
                    ),
                    normalized_at TEXT NOT NULL,
                    normalizer_version TEXT NOT NULL,
                    UNIQUE(item_kind, item_id, quality_report_id)
                );

                CREATE INDEX IF NOT EXISTS idx_normalized_quote_event
                ON normalized_quotes(symbol, event_time, item_id);

                CREATE INDEX IF NOT EXISTS idx_normalized_candle_event
                ON normalized_candles(interval, event_time, symbol, item_id);

                CREATE INDEX IF NOT EXISTS idx_normalized_candle_symbol
                ON normalized_candles(symbol, interval, event_time, item_id);

                CREATE INDEX IF NOT EXISTS idx_normalized_lineage_item
                ON normalization_lineage(item_kind, item_id, normalized_at);
                """
            )
            row = connection.execute(
                "SELECT value FROM normalized_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO normalized_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(_SCHEMA_VERSION)),
                )
            elif row[0] != str(_SCHEMA_VERSION):
                raise NormalizedMarketDataConfigurationError(
                    "Normalized-store schema version is unsupported."
                )
        try:
            os.chmod(self._catalog_path, 0o600)
        except OSError as error:
            raise NormalizedMarketDataIntegrityError(
                "Could not secure normalized storage catalog."
            ) from error

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        try:
            connection = sqlite3.connect(self._catalog_path, timeout=30.0)
        except sqlite3.Error as error:
            raise NormalizedMarketDataIntegrityError(
                "Could not open normalized storage catalog."
            ) from error
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _deterministic_item_id(
        item_kind: NormalizedDataKind,
        *parts: str,
    ) -> str:
        payload = "|".join((item_kind.value, *parts))
        return str(uuid5(_ITEM_NAMESPACE, payload))

    @staticmethod
    def _deterministic_lineage_id(
        item_kind: NormalizedDataKind,
        item_id: str,
        report_id: str,
    ) -> str:
        payload = f"{item_kind.value}|{item_id}|{report_id}"
        return str(uuid5(_LINEAGE_NAMESPACE, payload))

    @staticmethod
    def _reject_duplicate_quote_keys(quotes: tuple[Quote, ...]) -> None:
        keys: set[tuple[str, str, datetime]] = set()
        for quote in quotes:
            if not isinstance(quote, Quote):
                raise NormalizedMarketDataConfigurationError(
                    "Quote batch contains an invalid item."
                )
            key = (quote.source, quote.symbol, quote.timestamp.astimezone(UTC))
            if key in keys:
                raise NormalizedMarketDataConfigurationError(
                    "Quote batch contains a duplicate natural key."
                )
            keys.add(key)

    @staticmethod
    def _validate_batch_size(size: int) -> None:
        if size > _MAX_BATCH_ITEMS:
            raise NormalizedMarketDataConfigurationError(
                "Normalized batch cannot exceed 100000 items."
            )

    @staticmethod
    def _normalize_symbol_filter(
        symbols: Sequence[str] | None,
    ) -> tuple[str, ...] | None:
        if symbols is None:
            return None
        if isinstance(symbols, str):
            raise NormalizedMarketDataConfigurationError(
                "Symbols filter must be a sequence of strings."
            )
        normalized = tuple(
            SQLiteNormalizedMarketDataStore._normalize_symbol(symbol)
            for symbol in symbols
        )
        return tuple(dict.fromkeys(normalized)) or None

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        if not isinstance(symbol, str) or not symbol.strip():
            raise NormalizedMarketDataConfigurationError(
                "Symbol must be a nonblank string."
            )
        return symbol.strip().upper()

    @staticmethod
    def _normalize_status_filter(
        statuses: Sequence[QualityStatus] | None,
    ) -> tuple[QualityStatus, ...] | None:
        if statuses is None:
            return None
        if isinstance(statuses, (str, bytes)):
            raise NormalizedMarketDataConfigurationError(
                "Quality-status filter must be a sequence."
            )
        normalized = tuple(statuses)
        if not normalized or not all(
            status in (QualityStatus.PASS, QualityStatus.WARNING)
            for status in normalized
        ):
            raise NormalizedMarketDataConfigurationError(
                "Quality-status filter must contain PASS or WARNING values."
            )
        return tuple(dict.fromkeys(normalized))

    @staticmethod
    def _validate_query_datetime(value: datetime | None, name: str) -> None:
        if value is not None and (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise NormalizedMarketDataConfigurationError(
                f"{name} must be timezone-aware or None."
            )

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 10_000
        ):
            raise NormalizedMarketDataConfigurationError(
                "Normalized query limit must be between 1 and 10000."
            )

    @staticmethod
    def _validate_item_id(item_id: str) -> None:
        if not isinstance(item_id, str):
            raise NormalizedMarketDataConfigurationError(
                "Item ID must be a UUID string."
            )
        try:
            parsed = UUID(item_id)
        except ValueError as error:
            raise NormalizedMarketDataConfigurationError(
                "Item ID must be a UUID string."
            ) from error
        if str(parsed) != item_id:
            raise NormalizedMarketDataConfigurationError(
                "Item ID must use canonical UUID form."
            )

    @staticmethod
    def _required_str(value: object, field_name: str) -> str:
        if not isinstance(value, str):
            raise NormalizedMarketDataIntegrityError(
                f"Normalized catalog {field_name} is invalid."
            )
        return value

    @staticmethod
    def _required_int(value: object, field_name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise NormalizedMarketDataIntegrityError(
                f"Normalized catalog {field_name} is invalid."
            )
        return value
