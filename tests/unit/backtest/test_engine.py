import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid5

from world_quant_system.backtest import (
    BacktestConfig,
    BacktestRunResult,
    BuyAndHoldStrategy,
    OrderStatus,
    SmaCrossoverStrategy,
    StrategyBacktestEngine,
)
from world_quant_system.backtest.simulation import InMemoryNormalizedCandleReader
from world_quant_system.data import NormalizedCandleRecord, QualityStatus
from world_quant_system.domain import Candle, CandleInterval
from world_quant_system.replay import ReplayConfig

_NAMESPACE = UUID("6c36c92f-42f6-5f31-8eaa-8ff0c58f5a01")


def _records(
    prices: tuple[tuple[str, str], ...],
    *,
    volume: int = 1_000_000,
) -> tuple[NormalizedCandleRecord, ...]:
    result: list[NormalizedCandleRecord] = []
    for index, (open_text, close_text) in enumerate(prices):
        timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
        open_price = Decimal(open_text)
        close_price = Decimal(close_text)
        candle = Candle(
            symbol="ABC",
            interval=CandleInterval.DAY_1,
            timestamp=timestamp,
            open_price=open_price,
            high_price=max(open_price, close_price),
            low_price=min(open_price, close_price),
            close_price=close_price,
            volume=volume,
            currency="USD",
            source="test",
        )
        result.append(
            NormalizedCandleRecord(
                item_id=str(uuid5(_NAMESPACE, f"item-{index}")),
                candle=candle,
                quality_status=QualityStatus.PASS,
                raw_record_id=str(uuid5(_NAMESPACE, f"raw-{index}")),
                raw_content_sha256=f"{index:064x}",
                quality_report_id=str(uuid5(_NAMESPACE, f"report-{index}")),
                normalized_at=timestamp + timedelta(hours=1),
                normalizer_version="1.0.0",
                content_sha256=f"{index + 1000:064x}",
                schema_version=1,
                lineage_count=1,
            )
        )
    return tuple(result)


def _config() -> BacktestConfig:
    return BacktestConfig(
        replay=ReplayConfig(
            symbols=("ABC",),
            interval=CandleInterval.DAY_1,
            page_size=2,
        ),
        initial_cash=Decimal("1200"),
        commission_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
        max_volume_participation=Decimal("1"),
    )


def test_buy_and_hold_fills_at_next_open_not_signal_close() -> None:
    records = _records((("10", "11"), ("12", "13"), ("14", "15")))
    result = asyncio.run(
        StrategyBacktestEngine(
            InMemoryNormalizedCandleReader(records),
            _config(),
            BuyAndHoldStrategy(),
        ).run()
    )
    assert len(result.signals) == 3
    assert len(result.fills) == 1
    assert result.fills[0].filled_at == records[1].candle.timestamp
    assert result.orders[0].order.requested_at == records[0].candle.timestamp
    assert result.fills[0].execution_price == Decimal("12")
    assert result.metrics.final_equity == Decimal("1500")
    assert result.metrics.total_return == Decimal("0.25")
    assert result.metrics.benchmark_return == Decimal("0.25")


def test_one_candle_expires_order_without_fill() -> None:
    records = _records((("10", "11"),))
    result = asyncio.run(
        StrategyBacktestEngine(
            InMemoryNormalizedCandleReader(records),
            _config(),
            BuyAndHoldStrategy(),
        ).run()
    )
    assert result.fills == ()
    assert result.orders[0].status is OrderStatus.EXPIRED
    assert result.metrics.total_return == Decimal("0")


def test_sma_signal_fills_only_on_later_candle() -> None:
    records = _records(
        (("10", "10"), ("11", "11"), ("12", "12"), ("13", "13"))
    )
    result = asyncio.run(
        StrategyBacktestEngine(
            InMemoryNormalizedCandleReader(records),
            _config(),
            SmaCrossoverStrategy(short_window=1, long_window=2),
        ).run()
    )
    assert result.fills[0].filled_at == records[2].candle.timestamp
    assert result.orders[0].order.requested_at == records[1].candle.timestamp


def test_identical_inputs_produce_identical_digest() -> None:
    records = _records((("10", "10"), ("11", "11"), ("12", "12")))

    async def run_once() -> BacktestRunResult:
        return await StrategyBacktestEngine(
            InMemoryNormalizedCandleReader(records),
            _config(),
            BuyAndHoldStrategy(),
        ).run()

    first = asyncio.run(run_once())
    second = asyncio.run(run_once())
    assert first.run_digest == second.run_digest
    assert first.replay_result.event_digest == second.replay_result.event_digest


def test_empty_replay_returns_unchanged_cash() -> None:
    result = asyncio.run(
        StrategyBacktestEngine(
            InMemoryNormalizedCandleReader(()),
            _config(),
            BuyAndHoldStrategy(),
        ).run()
    )
    assert result.replay_result.event_count == 0
    assert result.equity_curve == ()
    assert result.signals == ()
    assert result.orders == ()
    assert result.fills == ()
    assert result.metrics.initial_equity == Decimal("1200")
    assert result.metrics.final_equity == Decimal("1200")
    assert result.metrics.total_return == Decimal("0")


def test_reused_sma_strategy_is_reset_between_engines() -> None:
    records = _records(
        (("10", "10"), ("11", "11"), ("12", "12"), ("13", "13"))
    )
    strategy = SmaCrossoverStrategy(short_window=1, long_window=2)

    async def run_once() -> BacktestRunResult:
        return await StrategyBacktestEngine(
            InMemoryNormalizedCandleReader(records),
            _config(),
            strategy,
        ).run()

    first = asyncio.run(run_once())
    second = asyncio.run(run_once())
    assert first.run_digest == second.run_digest
    assert first.orders == second.orders
    assert first.fills == second.fills


def test_integrated_buy_and_hold_includes_commission_and_slippage() -> None:
    records = _records((("10", "10"), ("10", "11"), ("12", "12")))
    config = BacktestConfig(
        replay=ReplayConfig(
            symbols=("ABC",),
            interval=CandleInterval.DAY_1,
            page_size=2,
        ),
        initial_cash=Decimal("1000"),
        commission_bps=Decimal("100"),
        slippage_bps=Decimal("50"),
        max_volume_participation=Decimal("1"),
    )
    result = asyncio.run(
        StrategyBacktestEngine(
            InMemoryNormalizedCandleReader(records),
            config,
            BuyAndHoldStrategy(),
        ).run()
    )
    fill = result.fills[0]
    assert fill.execution_price == Decimal("10.05")
    assert fill.quantity == 98
    assert fill.commission == Decimal("9.8490")
    assert fill.slippage_cost == Decimal("4.90")
    assert result.metrics.final_equity == Decimal("1181.2510")
    assert result.metrics.commission_cost == Decimal("9.8490")
    assert result.metrics.slippage_cost == Decimal("4.90")


def test_partial_fills_record_requested_and_filled_quantities() -> None:
    records = _records(
        (("10", "10"), ("10", "10"), ("10", "10")),
        volume=2,
    )
    config = BacktestConfig(
        replay=ReplayConfig(
            symbols=("ABC",),
            interval=CandleInterval.DAY_1,
            page_size=2,
        ),
        initial_cash=Decimal("1000"),
        commission_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
        max_volume_participation=Decimal("0.5"),
    )
    result = asyncio.run(
        StrategyBacktestEngine(
            InMemoryNormalizedCandleReader(records),
            config,
            BuyAndHoldStrategy(),
        ).run()
    )
    assert len(result.fills) == 2
    assert result.orders[0].status is OrderStatus.PARTIALLY_FILLED
    assert result.orders[0].requested_quantity == 100
    assert result.orders[0].quantity == 1
    assert result.orders[1].status is OrderStatus.PARTIALLY_FILLED
    assert result.orders[1].requested_quantity == 99
    assert result.orders[1].quantity == 1
    assert result.equity_curve[-1].quantity == 2


def test_future_price_mutation_cannot_change_past_state() -> None:
    baseline_records = _records(
        (
            ("10", "10"),
            ("11", "11"),
            ("12", "12"),
            ("13", "13"),
            ("14", "14"),
        )
    )
    mutated_records = _records(
        (
            ("10", "10"),
            ("11", "11"),
            ("12", "12"),
            ("130", "1"),
            ("140", "1"),
        )
    )
    cutoff = baseline_records[2].candle.timestamp

    def run(records: tuple[NormalizedCandleRecord, ...]) -> BacktestRunResult:
        return asyncio.run(
            StrategyBacktestEngine(
                InMemoryNormalizedCandleReader(records),
                _config(),
                SmaCrossoverStrategy(short_window=1, long_window=2),
            ).run()
        )

    baseline = run(baseline_records)
    mutated = run(mutated_records)

    assert tuple(
        signal for signal in baseline.signals if signal.generated_at <= cutoff
    ) == tuple(
        signal for signal in mutated.signals if signal.generated_at <= cutoff
    )
    assert tuple(
        record.order
        for record in baseline.orders
        if record.order.requested_at <= cutoff
    ) == tuple(
        record.order
        for record in mutated.orders
        if record.order.requested_at <= cutoff
    )
    assert tuple(
        fill for fill in baseline.fills if fill.filled_at <= cutoff
    ) == tuple(
        fill for fill in mutated.fills if fill.filled_at <= cutoff
    )
    assert tuple(
        point for point in baseline.equity_curve if point.timestamp <= cutoff
    ) == tuple(
        point for point in mutated.equity_curve if point.timestamp <= cutoff
    )

    assert baseline.run_digest != mutated.run_digest
    assert baseline.signals[-1] != mutated.signals[-1]
