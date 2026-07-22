import asyncio
import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from world_quant_system.data import (
    DataQualityReport,
    NormalizedCandleCursor,
    NormalizedDataKind,
    NormalizedMarketDataConflictError,
    NormalizedMarketDataIntegrityError,
    NormalizedMarketDataRejectedError,
    QualityDatasetKind,
    QualityIssue,
    QualityIssueCode,
    QualitySeverity,
    QualityStatus,
    SQLiteNormalizedMarketDataStore,
)
from world_quant_system.domain import Candle, CandleInterval, CandlePage, Quote


def report(
    *,
    kind: QualityDatasetKind,
    item_count: int,
    status: QualityStatus = QualityStatus.PASS,
) -> DataQualityReport:
    issues: tuple[QualityIssue, ...] = ()
    if status is QualityStatus.WARNING:
        issues = (
            QualityIssue(
                code=QualityIssueCode.EXTREME_PRICE_MOVE,
                severity=QualitySeverity.WARNING,
                message="Synthetic warning.",
            ),
        )
    elif status is QualityStatus.QUARANTINE:
        issues = (
            QualityIssue(
                code=QualityIssueCode.RAW_INTEGRITY_FAILURE,
                severity=QualitySeverity.QUARANTINE,
                message="Synthetic quarantine.",
            ),
        )
    return DataQualityReport(
        report_id=str(uuid4()),
        assessment_key=hashlib.sha256(str(uuid4()).encode()).hexdigest(),
        record_id=str(uuid4()),
        raw_content_sha256=hashlib.sha256(str(uuid4()).encode()).hexdigest(),
        dataset_kind=kind,
        status=status,
        checked_at=datetime(2026, 7, 21, tzinfo=UTC),
        validator_version="1.0.0",
        policy_fingerprint=hashlib.sha256(b"policy").hexdigest(),
        item_count=item_count,
        issues=issues,
    )


def candle(
    *,
    symbol: str = "005930",
    timestamp: datetime | None = None,
    close: Decimal = Decimal("95000"),
) -> Candle:
    event_time = timestamp or datetime(2026, 7, 20, tzinfo=UTC)
    return Candle(
        symbol=symbol,
        interval=CandleInterval.DAY_1,
        timestamp=event_time,
        open_price=close,
        high_price=close + Decimal("1000"),
        low_price=close - Decimal("1000"),
        close_price=close,
        volume=1_000_000,
        currency="KRW",
        source="toss",
    )


def quote() -> Quote:
    return Quote(
        symbol="005930",
        price=Decimal("95000"),
        timestamp=datetime(2026, 7, 21, tzinfo=UTC),
        currency="KRW",
        source="toss",
    )


@pytest.mark.asyncio
async def test_save_and_read_quote_with_lineage(tmp_path: Path) -> None:
    store = SQLiteNormalizedMarketDataStore(tmp_path / "normalized")
    quality_report = report(kind=QualityDatasetKind.QUOTES, item_count=1)

    records = await store.save_quotes(
        (quote(),),
        quality_report,
        normalized_at=datetime(2026, 7, 21, tzinfo=UTC),
        normalizer_version="1.0.0",
    )
    loaded = await store.get_quote(records[0].item_id)
    lineages = await store.get_lineages(NormalizedDataKind.QUOTE, loaded.item_id)

    assert loaded.quote == quote()
    assert loaded.lineage_count == 1
    assert lineages[0].quality_report_id == quality_report.report_id


@pytest.mark.asyncio
async def test_candle_write_is_idempotent_and_adds_distinct_lineage(
    tmp_path: Path,
) -> None:
    store = SQLiteNormalizedMarketDataStore(tmp_path / "normalized")
    page = CandlePage((candle(),), None)
    first_report = report(kind=QualityDatasetKind.CANDLES, item_count=1)
    second_report = report(
        kind=QualityDatasetKind.CANDLES,
        item_count=1,
        status=QualityStatus.WARNING,
    )
    timestamp = datetime(2026, 7, 21, tzinfo=UTC)

    first = await store.save_candle_page(
        page,
        first_report,
        normalized_at=timestamp,
        normalizer_version="1.0.0",
    )
    duplicate = await store.save_candle_page(
        page,
        first_report,
        normalized_at=timestamp,
        normalizer_version="1.0.0",
    )
    second = await store.save_candle_page(
        page,
        second_report,
        normalized_at=timestamp,
        normalizer_version="1.0.0",
    )

    assert first[0].item_id == duplicate[0].item_id == second[0].item_id
    assert second[0].lineage_count == 2
    assert second[0].quality_status is QualityStatus.WARNING


@pytest.mark.asyncio
async def test_conflicting_candle_rewrite_is_blocked(tmp_path: Path) -> None:
    store = SQLiteNormalizedMarketDataStore(tmp_path / "normalized")
    original = candle()
    changed = candle(close=Decimal("96000"))
    normalized_at = datetime(2026, 7, 21, tzinfo=UTC)

    await store.save_candle_page(
        CandlePage((original,), None),
        report(kind=QualityDatasetKind.CANDLES, item_count=1),
        normalized_at=normalized_at,
        normalizer_version="1.0.0",
    )
    with pytest.raises(NormalizedMarketDataConflictError):
        await store.save_candle_page(
            CandlePage((changed,), None),
            report(kind=QualityDatasetKind.CANDLES, item_count=1),
            normalized_at=normalized_at,
            normalizer_version="1.0.0",
        )


@pytest.mark.asyncio
async def test_quarantine_never_enters_normalized_storage(tmp_path: Path) -> None:
    store = SQLiteNormalizedMarketDataStore(tmp_path / "normalized")

    with pytest.raises(NormalizedMarketDataRejectedError):
        await store.save_candle_page(
            CandlePage((candle(),), None),
            report(
                kind=QualityDatasetKind.CANDLES,
                item_count=1,
                status=QualityStatus.QUARANTINE,
            ),
            normalized_at=datetime(2026, 7, 21, tzinfo=UTC),
            normalizer_version="1.0.0",
        )


@pytest.mark.asyncio
async def test_query_candles_is_deterministic_and_pageable(tmp_path: Path) -> None:
    store = SQLiteNormalizedMarketDataStore(tmp_path / "normalized")
    start = datetime(2026, 7, 1, tzinfo=UTC)
    first_symbol = (
        candle(symbol="005930", timestamp=start),
        candle(symbol="005930", timestamp=start + timedelta(days=2)),
    )
    second_symbol = (
        candle(symbol="000660", timestamp=start + timedelta(days=1)),
    )
    normalized_at = datetime(2026, 7, 21, tzinfo=UTC)
    await store.save_candle_page(
        CandlePage(first_symbol, None),
        report(kind=QualityDatasetKind.CANDLES, item_count=2),
        normalized_at=normalized_at,
        normalizer_version="1.0.0",
    )
    await store.save_candle_page(
        CandlePage(second_symbol, None),
        report(kind=QualityDatasetKind.CANDLES, item_count=1),
        normalized_at=normalized_at,
        normalizer_version="1.0.0",
    )
    saved = await store.query_candles(limit=10)

    first_page = await store.query_candles(limit=2)
    cursor_record = first_page[-1]
    second_page = await store.query_candles(
        after=NormalizedCandleCursor(
            timestamp=cursor_record.candle.timestamp,
            symbol=cursor_record.candle.symbol,
            item_id=cursor_record.item_id,
        ),
        limit=2,
    )

    assert tuple(first_page) + tuple(second_page) == saved


@pytest.mark.asyncio
async def test_concurrent_identical_writers_create_one_item(tmp_path: Path) -> None:
    store = SQLiteNormalizedMarketDataStore(tmp_path / "normalized")
    quality_report = report(kind=QualityDatasetKind.CANDLES, item_count=1)
    page = CandlePage((candle(),), None)
    normalized_at = datetime(2026, 7, 21, tzinfo=UTC)

    results = await asyncio.gather(
        *(
            store.save_candle_page(
                page,
                quality_report,
                normalized_at=normalized_at,
                normalizer_version="1.0.0",
            )
            for _ in range(100)
        )
    )

    assert len({result[0].item_id for result in results}) == 1
    assert results[-1][0].lineage_count == 1


@pytest.mark.asyncio
async def test_catalog_tampering_is_detected(tmp_path: Path) -> None:
    store = SQLiteNormalizedMarketDataStore(tmp_path / "normalized")
    records = await store.save_candle_page(
        CandlePage((candle(),), None),
        report(kind=QualityDatasetKind.CANDLES, item_count=1),
        normalized_at=datetime(2026, 7, 21, tzinfo=UTC),
        normalizer_version="1.0.0",
    )
    with sqlite3.connect(store.root / "normalized.sqlite3") as connection:
        connection.execute(
            "UPDATE normalized_candles SET content_json = '{}' WHERE item_id = ?",
            (records[0].item_id,),
        )

    with pytest.raises(NormalizedMarketDataIntegrityError):
        await store.get_candle(records[0].item_id)


@pytest.mark.asyncio
async def test_sqlite_connections_are_closed(tmp_path: Path, monkeypatch: Any) -> None:
    store = SQLiteNormalizedMarketDataStore(tmp_path / "normalized")
    original_connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []

    def tracking_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = cast(sqlite3.Connection, original_connect(*args, **kwargs))
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    await store.save_candle_page(
        CandlePage((candle(),), None),
        report(kind=QualityDatasetKind.CANDLES, item_count=1),
        normalized_at=datetime(2026, 7, 21, tzinfo=UTC),
        normalizer_version="1.0.0",
    )
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
