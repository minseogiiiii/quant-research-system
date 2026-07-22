import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from world_quant_system.data import (
    DataQualityReport,
    QualityDatasetKind,
    QualityIssue,
    QualityIssueCode,
    QualitySeverity,
    QualityStatus,
    SQLiteNormalizedMarketDataStore,
)
from world_quant_system.domain import Candle, CandleInterval, CandlePage
from world_quant_system.replay import (
    DeterministicReplayEngine,
    ReplayClock,
    ReplayConfig,
    ReplayConfigurationError,
    ReplayEvent,
    ReplayInvariantError,
)


class CollectingHandler:
    def __init__(self) -> None:
        self.events: list[ReplayEvent] = []

    async def on_candle(self, event: ReplayEvent, clock: ReplayClock) -> None:
        assert clock.current == event.event_time
        self.events.append(event)


def report(item_count: int, status: QualityStatus) -> DataQualityReport:
    issues: tuple[QualityIssue, ...] = ()
    if status is QualityStatus.WARNING:
        issues = (
            QualityIssue(
                code=QualityIssueCode.CANDLE_GAP,
                severity=QualitySeverity.WARNING,
                message="Synthetic warning.",
            ),
        )
    return DataQualityReport(
        report_id=str(uuid4()),
        assessment_key=hashlib.sha256(str(uuid4()).encode()).hexdigest(),
        record_id=str(uuid4()),
        raw_content_sha256=hashlib.sha256(str(uuid4()).encode()).hexdigest(),
        dataset_kind=QualityDatasetKind.CANDLES,
        status=status,
        checked_at=datetime(2026, 7, 21, tzinfo=UTC),
        validator_version="1.0.0",
        policy_fingerprint=hashlib.sha256(b"policy").hexdigest(),
        item_count=item_count,
        issues=issues,
    )


def candles(symbol: str, start: datetime, count: int) -> tuple[Candle, ...]:
    price = Decimal("95000")
    return tuple(
        Candle(
            symbol=symbol,
            interval=CandleInterval.DAY_1,
            timestamp=start + timedelta(days=index),
            open_price=price,
            high_price=price + Decimal("100"),
            low_price=price - Decimal("100"),
            close_price=price,
            volume=1_000 + index,
            currency="KRW",
            source="toss",
        )
        for index in range(count)
    )


@pytest.mark.asyncio
async def test_replay_is_deterministic_and_preserves_warning_data(
    tmp_path: Path,
) -> None:
    store = SQLiteNormalizedMarketDataStore(tmp_path / "normalized")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    pass_page = CandlePage(candles("005930", start, 5), None)
    warning_page = CandlePage(candles("000660", start, 3), None)
    normalized_at = datetime(2026, 7, 21, tzinfo=UTC)
    await store.save_candle_page(
        pass_page,
        report(5, QualityStatus.PASS),
        normalized_at=normalized_at,
        normalizer_version="1.0.0",
    )
    await store.save_candle_page(
        warning_page,
        report(3, QualityStatus.WARNING),
        normalized_at=normalized_at,
        normalizer_version="1.0.0",
    )

    config = ReplayConfig(
        symbols=("005930", "000660"),
        interval=CandleInterval.DAY_1,
        page_size=2,
    )
    handler = CollectingHandler()
    first = await DeterministicReplayEngine(store, config).run(handler)
    second = await DeterministicReplayEngine(store, config).run()

    assert first.event_count == 8
    assert first.warning_count == 3
    assert first.event_digest == second.event_digest
    assert [event.sequence for event in handler.events] == list(range(8))


@pytest.mark.asyncio
async def test_replay_can_exclude_warning_data(tmp_path: Path) -> None:
    store = SQLiteNormalizedMarketDataStore(tmp_path / "normalized")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    await store.save_candle_page(
        CandlePage(candles("005930", start, 2), None),
        report(2, QualityStatus.WARNING),
        normalized_at=datetime(2026, 7, 21, tzinfo=UTC),
        normalizer_version="1.0.0",
    )

    result = await DeterministicReplayEngine(
        store,
        ReplayConfig(
            symbols=("005930",),
            interval=CandleInterval.DAY_1,
            include_warnings=False,
        ),
    ).run()

    assert result.event_count == 0


def test_replay_clock_cannot_move_backwards() -> None:
    clock = ReplayClock()
    clock.advance_to(datetime(2026, 1, 2, tzinfo=UTC))

    with pytest.raises(ReplayInvariantError):
        clock.advance_to(datetime(2026, 1, 1, tzinfo=UTC))


@pytest.mark.asyncio
async def test_replay_engine_is_single_use(tmp_path: Path) -> None:
    store = SQLiteNormalizedMarketDataStore(tmp_path / "normalized")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    await store.save_candle_page(
        CandlePage(candles("005930", start, 1), None),
        report(1, QualityStatus.PASS),
        normalized_at=datetime(2026, 7, 21, tzinfo=UTC),
        normalizer_version="1.0.0",
    )
    engine = DeterministicReplayEngine(
        store,
        ReplayConfig(
            symbols=("005930",),
            interval=CandleInterval.DAY_1,
        ),
    )

    await engine.run()
    with pytest.raises(ReplayConfigurationError, match="single-use"):
        await engine.run()
