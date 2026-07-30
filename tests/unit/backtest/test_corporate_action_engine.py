import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid5

from world_quant_system.backtest import (
    BacktestConfig,
    BuyAndHoldStrategy,
    CorporateActionTimeline,
    FlatRateDividendTaxModel,
    OrderRejectReason,
    StrategyBacktestEngine,
)
from world_quant_system.backtest.simulation import InMemoryNormalizedCandleReader
from world_quant_system.data import NormalizedCandleRecord, QualityStatus
from world_quant_system.data.normalized_models import canonical_json_bytes
from world_quant_system.domain import Candle, CandleInterval
from world_quant_system.replay import ReplayConfig
from world_quant_system.research import (
    CorporateActionBacktestContext,
    CorporateActionPolicy,
    CorporateActionRecord,
    CorporateActionType,
)

_NAMESPACE = UUID("a584bd11-e6d0-5d21-8eb3-dc1d21e5efda")
START = datetime(2020, 1, 1, tzinfo=UTC)


def record(symbol: str, offset: int, price: str) -> NormalizedCandleRecord:
    timestamp = START + timedelta(days=offset)
    decimal_price = Decimal(price)
    candle = Candle(
        symbol=symbol,
        interval=CandleInterval.DAY_1,
        timestamp=timestamp,
        open_price=decimal_price,
        high_price=decimal_price,
        low_price=decimal_price,
        close_price=decimal_price,
        volume=1_000_000,
        currency="USD",
        source="test",
    )
    return NormalizedCandleRecord(
        item_id=str(uuid5(_NAMESPACE, f"item|{symbol}|{offset}")),
        candle=candle,
        quality_status=QualityStatus.PASS,
        raw_record_id=str(uuid5(_NAMESPACE, f"raw|{symbol}|{offset}")),
        raw_content_sha256=f"{offset + 1:064x}",
        quality_report_id=str(
            uuid5(_NAMESPACE, f"report|{symbol}|{offset}")
        ),
        normalized_at=timestamp + timedelta(hours=1),
        normalizer_version="1.0.0",
        content_sha256=f"{offset + 100:064x}",
        schema_version=1,
        lineage_count=1,
    )


def context_for(
    action: CorporateActionRecord,
    *,
    symbols: tuple[str, ...],
) -> CorporateActionBacktestContext:
    documents = [action.to_document()]
    dataset_digest = hashlib.sha256(
        canonical_json_bytes(documents)
    ).hexdigest()
    return CorporateActionBacktestContext(
        exchange=action.exchange,
        initial_symbol=symbols[0],
        start=START,
        end=START + timedelta(days=4),
        policy=CorporateActionPolicy(),
        action_ids=(action.action_id,),
        symbols=symbols,
        action_dataset_digest=dataset_digest,
    )


def test_split_is_applied_before_same_day_candle_and_preserves_equity() -> None:
    records = tuple(
        record("ABC", offset, price)
        for offset, price in enumerate(("10", "10", "5", "5"))
    )
    action = CorporateActionRecord(
        exchange="XNAS",
        symbol="ABC",
        action_type=CorporateActionType.SPLIT,
        effective_at=START + timedelta(days=2),
        available_at=START,
        source="test",
        source_digest="a" * 64,
        ratio_numerator=2,
        ratio_denominator=1,
    )
    context = context_for(action, symbols=("ABC",))
    timeline = CorporateActionTimeline(context, (action,))
    tax_model = FlatRateDividendTaxModel()
    config = BacktestConfig(
        replay=ReplayConfig(
            symbols=("ABC",),
            interval=CandleInterval.DAY_1,
            start=START,
            end=START + timedelta(days=3),
        ),
        initial_cash=Decimal("1000"),
        commission_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
        max_volume_participation=Decimal("1"),
        corporate_action_context_digest=context.context_digest,
        dividend_tax_model_digest=tax_model.fingerprint,
    )

    result = asyncio.run(
        StrategyBacktestEngine(
            InMemoryNormalizedCandleReader(records),
            config,
            BuyAndHoldStrategy(),
            corporate_action_timeline=timeline,
            dividend_tax_model=tax_model,
        ).run()
    )

    assert result.equity_curve[-1].quantity == 200
    assert result.equity_curve[-1].total_equity == Decimal("1000")
    assert len(result.corporate_actions) == 1
    assert result.metrics.benchmark_return is None


def test_symbol_change_cancels_stale_pending_order() -> None:
    records = (
        record("OLD", 0, "10"),
        record("OLD", 1, "10"),
        record("NEW", 2, "10"),
        record("NEW", 3, "10"),
    )
    action = CorporateActionRecord(
        exchange="XNAS",
        symbol="OLD",
        action_type=CorporateActionType.SYMBOL_CHANGE,
        effective_at=START + timedelta(days=2),
        available_at=START,
        source="test",
        source_digest="b" * 64,
        new_symbol="NEW",
    )
    context = context_for(action, symbols=("OLD", "NEW"))
    timeline = CorporateActionTimeline(context, (action,))
    tax_model = FlatRateDividendTaxModel()
    config = BacktestConfig(
        replay=ReplayConfig(
            symbols=("OLD", "NEW"),
            interval=CandleInterval.DAY_1,
            start=START,
            end=START + timedelta(days=3),
        ),
        initial_symbol="OLD",
        initial_cash=Decimal("1000"),
        commission_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
        max_volume_participation=Decimal("0.00005"),
        corporate_action_context_digest=context.context_digest,
        dividend_tax_model_digest=tax_model.fingerprint,
    )

    result = asyncio.run(
        StrategyBacktestEngine(
            InMemoryNormalizedCandleReader(records),
            config,
            BuyAndHoldStrategy(),
            corporate_action_timeline=timeline,
            dividend_tax_model=tax_model,
        ).run()
    )

    assert result.equity_curve[-1].symbol == "NEW"
    assert result.equity_curve[-1].quantity == 100
    assert any(
        order.reject_reason is OrderRejectReason.CORPORATE_ACTION_BOUNDARY
        for order in result.orders
    )
    assert result.run_digest

def test_post_replay_delisting_cancels_pending_order_at_boundary() -> None:
    records = (
        record("ABC", 0, "10"),
        record("ABC", 1, "10"),
    )
    action = CorporateActionRecord(
        exchange="XNAS",
        symbol="ABC",
        action_type=CorporateActionType.DELISTING,
        effective_at=START + timedelta(days=2),
        available_at=START,
        source="test",
        source_digest="c" * 64,
        delisting_cash_price=Decimal("0"),
    )
    context = context_for(action, symbols=("ABC",))
    timeline = CorporateActionTimeline(context, (action,))
    tax_model = FlatRateDividendTaxModel()
    config = BacktestConfig(
        replay=ReplayConfig(
            symbols=("ABC",),
            interval=CandleInterval.DAY_1,
            start=START,
            end=START + timedelta(days=2),
        ),
        initial_cash=Decimal("1000"),
        commission_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
        max_volume_participation=Decimal("0.00005"),
        corporate_action_context_digest=context.context_digest,
        dividend_tax_model_digest=tax_model.fingerprint,
    )

    result = asyncio.run(
        StrategyBacktestEngine(
            InMemoryNormalizedCandleReader(records),
            config,
            BuyAndHoldStrategy(),
            corporate_action_timeline=timeline,
            dividend_tax_model=tax_model,
        ).run()
    )

    assert result.equity_curve[-1].quantity == 0
    assert result.equity_curve[-1].total_equity == Decimal("500")
    assert result.orders[-1].reject_reason is (
        OrderRejectReason.CORPORATE_ACTION_BOUNDARY
    )
