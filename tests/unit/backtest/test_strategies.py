from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from world_quant_system.backtest import (
    BuyAndHoldStrategy,
    LongOnlyPortfolioLedger,
    PortfolioSnapshot,
    SmaCrossoverStrategy,
)
from world_quant_system.data import NormalizedCandleRecord, QualityStatus
from world_quant_system.domain import Candle, CandleInterval
from world_quant_system.replay import ReplayEvent


def _event(index: int, close: str) -> ReplayEvent:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
    price = Decimal(close)
    candle = Candle(
        symbol="ABC",
        interval=CandleInterval.DAY_1,
        timestamp=timestamp,
        open_price=price,
        high_price=price,
        low_price=price,
        close_price=price,
        volume=1000,
        currency="USD",
        source="test",
    )
    record = NormalizedCandleRecord(
        item_id=str(uuid4()),
        candle=candle,
        quality_status=QualityStatus.PASS,
        raw_record_id=str(uuid4()),
        raw_content_sha256="0" * 64,
        quality_report_id=str(uuid4()),
        normalized_at=timestamp + timedelta(hours=1),
        normalizer_version="1.0.0",
        content_sha256="1" * 64,
        schema_version=1,
        lineage_count=1,
    )
    return ReplayEvent(sequence=index, event_time=timestamp, record=record)


def _snapshot(event: ReplayEvent) -> PortfolioSnapshot:
    return LongOnlyPortfolioLedger("ABC", Decimal("1000")).snapshot(
        timestamp=event.event_time,
        market_price=event.record.candle.close_price,
    )


def test_buy_and_hold_requests_long_target() -> None:
    event = _event(0, "10")
    signal = BuyAndHoldStrategy().on_candle(event, _snapshot(event))
    assert signal.target_fraction == Decimal("1")
    assert signal.generated_at == event.event_time


def test_sma_crossover_uses_only_observed_closes() -> None:
    strategy = SmaCrossoverStrategy(short_window=2, long_window=3)
    targets = []
    for index, close in enumerate(("10", "11", "12", "9")):
        event = _event(index, close)
        targets.append(strategy.on_candle(event, _snapshot(event)).target_fraction)
    assert targets == [
        Decimal("0"),
        Decimal("0"),
        Decimal("1"),
        Decimal("0"),
    ]
