from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid5

from world_quant_system.backtest.engine import StrategyBacktestEngine
from world_quant_system.backtest.models import BacktestConfig, BacktestRunResult
from world_quant_system.backtest.strategies import (
    BuyAndHoldStrategy,
    SmaCrossoverStrategy,
)
from world_quant_system.data.normalized_models import (
    NormalizedCandleCursor,
    NormalizedCandleRecord,
    candle_document,
    canonical_json_bytes,
)
from world_quant_system.data.quality_models import QualityStatus
from world_quant_system.domain import Candle, CandleInterval
from world_quant_system.replay import ReplayConfig

_ITEM_NAMESPACE = UUID("9bd64584-cad5-5528-b3d9-cdb5568d07c6")
_RAW_NAMESPACE = UUID("5e01e6b7-ecfd-54d2-9491-644e1e94450f")
_REPORT_NAMESPACE = UUID("55a8010c-9fe3-544e-b0b4-001ef4e7580a")


@dataclass(frozen=True, slots=True)
class StrategyBacktestSimulationResult:
    candle_count: int
    buy_and_hold: BacktestRunResult
    sma_crossover: BacktestRunResult
    deterministic_digest_match: bool
    look_ahead_violations: int


class InMemoryNormalizedCandleReader:
    def __init__(self, records: tuple[NormalizedCandleRecord, ...]) -> None:
        self._records = tuple(
            sorted(
                records,
                key=lambda item: (
                    item.candle.timestamp,
                    item.candle.symbol,
                    item.item_id,
                ),
            )
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
        selected: list[NormalizedCandleRecord] = []
        symbol_set = None if symbols is None else set(symbols)
        status_set = None if statuses is None else set(statuses)
        after_key = (
            None
            if after is None
            else (after.timestamp, after.symbol, after.item_id)
        )
        for record in self._records:
            candle = record.candle
            key = (candle.timestamp, candle.symbol, record.item_id)
            if symbol_set is not None and candle.symbol not in symbol_set:
                continue
            if interval is not None and candle.interval is not interval:
                continue
            if start is not None and candle.timestamp < start:
                continue
            if end is not None and candle.timestamp > end:
                continue
            if status_set is not None and record.quality_status not in status_set:
                continue
            if after_key is not None and key <= after_key:
                continue
            selected.append(record)
            if len(selected) == limit:
                break
        return tuple(selected)


def run_strategy_backtest_simulation() -> StrategyBacktestSimulationResult:
    return asyncio.run(_run_simulation())


async def _run_simulation() -> StrategyBacktestSimulationResult:
    records = build_synthetic_records(480)
    reader = InMemoryNormalizedCandleReader(records)
    buy_config = BacktestConfig(
        replay=ReplayConfig(
            symbols=("005930",),
            interval=CandleInterval.DAY_1,
            page_size=37,
        ),
        initial_cash=Decimal("10000000"),
        commission_bps=Decimal("15"),
        slippage_bps=Decimal("10"),
        max_volume_participation=Decimal("0.10"),
    )
    buy_and_hold = await StrategyBacktestEngine(
        reader,
        buy_config,
        BuyAndHoldStrategy(),
    ).run()
    repeated = await StrategyBacktestEngine(
        reader,
        buy_config,
        BuyAndHoldStrategy(),
    ).run()
    sma_crossover = await StrategyBacktestEngine(
        reader,
        BacktestConfig(
            replay=ReplayConfig(
                symbols=("005930",),
                interval=CandleInterval.DAY_1,
                page_size=53,
            ),
            initial_cash=Decimal("10000000"),
            commission_bps=Decimal("15"),
            slippage_bps=Decimal("10"),
            max_volume_participation=Decimal("0.10"),
        ),
        SmaCrossoverStrategy(short_window=20, long_window=80),
    ).run()
    violations = sum(
        1
        for order in buy_and_hold.orders + sma_crossover.orders
        if order.fill_id is not None
        and next(
            fill.filled_at
            for fill in buy_and_hold.fills + sma_crossover.fills
            if fill.fill_id == order.fill_id
        )
        <= order.order.requested_at
    )
    return StrategyBacktestSimulationResult(
        candle_count=len(records),
        buy_and_hold=buy_and_hold,
        sma_crossover=sma_crossover,
        deterministic_digest_match=(buy_and_hold.run_digest == repeated.run_digest),
        look_ahead_violations=violations,
    )


def build_synthetic_records(count: int) -> tuple[NormalizedCandleRecord, ...]:
    records: list[NormalizedCandleRecord] = []
    price = Decimal("50000")
    start = datetime(2024, 1, 1, tzinfo=UTC)
    for index in range(count):
        regime = (index // 80) % 4
        if regime in (0, 3):
            price += Decimal("180")
        elif regime == 1:
            price -= Decimal("140")
        else:
            price += Decimal("40") if index % 2 == 0 else Decimal("-20")
        open_price = price - Decimal("25")
        close_price = price
        high_price = max(open_price, close_price) + Decimal("75")
        low_price = min(open_price, close_price) - Decimal("75")
        timestamp = start + timedelta(days=index)
        candle = Candle(
            symbol="005930",
            interval=CandleInterval.DAY_1,
            timestamp=timestamp,
            open_price=open_price,
            high_price=high_price,
            low_price=low_price,
            close_price=close_price,
            volume=1_000_000,
            currency="KRW",
            source="simulation",
        )
        document = candle_document(candle)
        content_sha256 = hashlib.sha256(canonical_json_bytes(document)).hexdigest()
        item_id = str(uuid5(_ITEM_NAMESPACE, f"005930|1d|{timestamp.isoformat()}"))
        raw_id = str(uuid5(_RAW_NAMESPACE, f"raw|{timestamp.isoformat()}"))
        report_id = str(uuid5(_REPORT_NAMESPACE, f"report|{timestamp.isoformat()}"))
        records.append(
            NormalizedCandleRecord(
                item_id=item_id,
                candle=candle,
                quality_status=(
                    QualityStatus.WARNING if index % 101 == 0 else QualityStatus.PASS
                ),
                raw_record_id=raw_id,
                raw_content_sha256=content_sha256,
                quality_report_id=report_id,
                normalized_at=timestamp + timedelta(hours=1),
                normalizer_version="1.0.0",
                content_sha256=content_sha256,
                schema_version=1,
                lineage_count=1,
            )
        )
    return tuple(records)
