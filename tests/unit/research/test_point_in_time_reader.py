from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid5

import pytest

from world_quant_system.backtest import BacktestConfig
from world_quant_system.backtest.simulation import InMemoryNormalizedCandleReader
from world_quant_system.data import NormalizedCandleRecord, QualityStatus
from world_quant_system.domain import Candle, CandleInterval
from world_quant_system.replay import ReplayConfig
from world_quant_system.research import (
    DataAvailabilityRecord,
    PointInTimeDataKind,
    PointInTimeEligibilityError,
    PointInTimeIntegrityError,
    PointInTimeValidatedCandleReader,
    PointInTimeValidatedMultiSymbolCandleReader,
    SecurityLifecycle,
    SQLitePointInTimeStore,
    UniverseMembership,
)

_NAMESPACE = UUID("f11238c5-31d1-50f0-a9b8-0e5cd0f954f2")
START = datetime(2020, 1, 1, tzinfo=UTC)


def records() -> tuple[NormalizedCandleRecord, ...]:
    result: list[NormalizedCandleRecord] = []
    for index in range(3):
        timestamp = START + timedelta(days=index + 1)
        price = Decimal(100 + index)
        result.append(
            NormalizedCandleRecord(
                item_id=str(uuid5(_NAMESPACE, f"item-{index}")),
                candle=Candle(
                    symbol="005930",
                    interval=CandleInterval.DAY_1,
                    timestamp=timestamp,
                    open_price=price,
                    high_price=price,
                    low_price=price,
                    close_price=price,
                    volume=1_000,
                    currency="KRW",
                    source="test",
                ),
                quality_status=QualityStatus.PASS,
                raw_record_id=str(uuid5(_NAMESPACE, f"raw-{index}")),
                raw_content_sha256=f"{index:064x}",
                quality_report_id=str(uuid5(_NAMESPACE, f"report-{index}")),
                normalized_at=timestamp + timedelta(hours=1),
                normalizer_version="1.0.0",
                content_sha256=f"{index + 100:064x}",
                schema_version=1,
                lineage_count=1,
            )
        )
    return tuple(result)


async def seeded_store(
    root: Path,
    candidate_records: tuple[NormalizedCandleRecord, ...],
    *,
    future_index: int | None = None,
) -> SQLitePointInTimeStore:
    store = SQLitePointInTimeStore(root)
    await store.save_security(
        SecurityLifecycle(
            exchange="XKRX",
            symbol="005930",
            listed_at=datetime(1975, 6, 11, tzinfo=UTC),
            tradable_from=datetime(1975, 6, 11, tzinfo=UTC),
            source="test",
            source_digest="a" * 64,
        )
    )
    await store.save_membership(
        UniverseMembership(
            universe_id="KOSPI",
            exchange="XKRX",
            symbol="005930",
            member_from=START,
            member_until=START + timedelta(days=10),
            available_at=START,
            source="test",
            source_digest="b" * 64,
        )
    )
    for index, record in enumerate(candidate_records):
        delay = timedelta(days=1) if index == future_index else timedelta(0)
        await store.save_availability(
            DataAvailabilityRecord(
                data_id=record.item_id,
                data_kind=PointInTimeDataKind.CANDLE,
                exchange="XKRX",
                symbol="005930",
                effective_at=record.candle.timestamp,
                available_at=record.candle.timestamp + delay,
                source="test",
                source_digest="c" * 64,
            )
        )
    return store


@pytest.mark.asyncio
async def test_validated_reader_exposes_only_eligible_candles(tmp_path: Path) -> None:
    candidate_records = records()
    store = await seeded_store(tmp_path / "pit", candidate_records)
    context = await store.build_backtest_context(
        universe_id="KOSPI",
        exchange="XKRX",
        symbol="005930",
        start=START,
        end=START + timedelta(days=5),
    )
    reader = PointInTimeValidatedCandleReader(
        InMemoryNormalizedCandleReader(candidate_records),
        store,
        context,
    )

    loaded = await reader.query_candles(limit=10)

    assert loaded == candidate_records
    assert reader.context_digest == context.context_digest


@pytest.mark.asyncio
async def test_validated_reader_blocks_future_available_candle(tmp_path: Path) -> None:
    candidate_records = records()
    store = await seeded_store(
        tmp_path / "pit",
        candidate_records,
        future_index=1,
    )
    context = await store.build_backtest_context(
        universe_id="KOSPI",
        exchange="XKRX",
        symbol="005930",
        start=START,
        end=START + timedelta(days=5),
    )
    reader = PointInTimeValidatedCandleReader(
        InMemoryNormalizedCandleReader(candidate_records),
        store,
        context,
    )

    with pytest.raises(PointInTimeEligibilityError):
        await reader.query_candles(limit=10)


def test_backtest_config_fingerprint_includes_optional_data_context_digest() -> None:
    replay = ReplayConfig(
        symbols=("005930",),
        interval=CandleInterval.DAY_1,
    )
    without_context = BacktestConfig(
        replay=replay,
        initial_cash=Decimal("1000"),
    )
    with_context = BacktestConfig(
        replay=replay,
        initial_cash=Decimal("1000"),
        data_context_digest="d" * 64,
    )

    assert without_context.fingerprint != with_context.fingerprint
    assert without_context.data_context_digest is None
    assert with_context.data_context_digest == "d" * 64


@pytest.mark.asyncio
async def test_validated_reader_rejects_metadata_added_after_context_build(
    tmp_path: Path,
) -> None:
    candidate_records = records()
    store = await seeded_store(tmp_path / "pit", candidate_records[:2])
    context = await store.build_backtest_context(
        universe_id="KOSPI",
        exchange="XKRX",
        symbol="005930",
        start=START,
        end=START + timedelta(days=5),
    )
    late_record = candidate_records[2]
    await store.save_availability(
        DataAvailabilityRecord(
            data_id=late_record.item_id,
            data_kind=PointInTimeDataKind.CANDLE,
            exchange="XKRX",
            symbol="005930",
            effective_at=late_record.candle.timestamp,
            available_at=late_record.candle.timestamp,
            source="test",
            source_digest="c" * 64,
        )
    )
    reader = PointInTimeValidatedCandleReader(
        InMemoryNormalizedCandleReader(candidate_records),
        store,
        context,
    )

    with pytest.raises(PointInTimeIntegrityError):
        await reader.query_candles(limit=10)


@pytest.mark.asyncio
async def test_multi_symbol_reader_validates_one_symbol_change_path(
    tmp_path: Path,
) -> None:
    old_timestamp = START + timedelta(days=1)
    new_timestamp = START + timedelta(days=3)

    def item(symbol: str, timestamp: datetime, identity: str) -> NormalizedCandleRecord:
        return NormalizedCandleRecord(
            item_id=str(uuid5(_NAMESPACE, f"item-{identity}")),
            candle=Candle(
                symbol=symbol,
                interval=CandleInterval.DAY_1,
                timestamp=timestamp,
                open_price=Decimal("10"),
                high_price=Decimal("10"),
                low_price=Decimal("10"),
                close_price=Decimal("10"),
                volume=1_000,
                currency="USD",
                source="test",
            ),
            quality_status=QualityStatus.PASS,
            raw_record_id=str(uuid5(_NAMESPACE, f"raw-{identity}")),
            raw_content_sha256="d" * 64,
            quality_report_id=str(uuid5(_NAMESPACE, f"report-{identity}")),
            normalized_at=timestamp + timedelta(hours=1),
            normalizer_version="1.0.0",
            content_sha256="e" * 64,
            schema_version=1,
            lineage_count=1,
        )

    candidate_records = (
        item("OLD", old_timestamp, "old"),
        item("NEW", new_timestamp, "new"),
    )
    store = SQLitePointInTimeStore(tmp_path / "pit")
    for symbol in ("OLD", "NEW"):
        await store.save_security(
            SecurityLifecycle(
                exchange="XNAS",
                symbol=symbol,
                listed_at=datetime(2000, 1, 1, tzinfo=UTC),
                tradable_from=datetime(2000, 1, 1, tzinfo=UTC),
                source="test",
                source_digest="a" * 64,
            )
        )
    for symbol, member_from, member_until in (
        ("OLD", START, START + timedelta(days=2)),
        (
            "NEW",
            START + timedelta(days=2),
            START + timedelta(days=5),
        ),
    ):
        await store.save_membership(
            UniverseMembership(
                universe_id="TEST",
                exchange="XNAS",
                symbol=symbol,
                member_from=member_from,
                member_until=member_until,
                available_at=START,
                source="test",
                source_digest="b" * 64,
            )
        )
    for record in candidate_records:
        await store.save_availability(
            DataAvailabilityRecord(
                data_id=record.item_id,
                data_kind=PointInTimeDataKind.CANDLE,
                exchange="XNAS",
                symbol=record.candle.symbol,
                effective_at=record.candle.timestamp,
                available_at=record.candle.timestamp,
                source="test",
                source_digest="c" * 64,
            )
        )
    old_context = await store.build_backtest_context(
        universe_id="TEST",
        exchange="XNAS",
        symbol="OLD",
        start=START,
        end=START + timedelta(days=2),
    )
    new_context = await store.build_backtest_context(
        universe_id="TEST",
        exchange="XNAS",
        symbol="NEW",
        start=START + timedelta(days=2),
        end=START + timedelta(days=5),
    )
    reader = PointInTimeValidatedMultiSymbolCandleReader(
        InMemoryNormalizedCandleReader(candidate_records),
        store,
        (old_context, new_context),
    )
    reversed_reader = PointInTimeValidatedMultiSymbolCandleReader(
        InMemoryNormalizedCandleReader(candidate_records),
        store,
        (new_context, old_context),
    )

    assert await reader.query_candles(limit=10) == candidate_records
    assert reader.context_digest == reversed_reader.context_digest
