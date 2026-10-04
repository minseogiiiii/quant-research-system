from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from world_quant_system.backtest import (
    BasisPointsCommissionModel,
    FixedBasisPointsSlippageModel,
    LongOnlyPortfolioLedger,
    NextOpenExecutionModel,
    OrderSide,
    TargetPositionOrder,
)
from world_quant_system.backtest.models import BacktestInvariantError
from world_quant_system.domain import Candle, CandleInterval


def _order(*, side: OrderSide, requested_at: datetime) -> TargetPositionOrder:
    target = Decimal("1") if side is OrderSide.BUY else Decimal("0")
    return TargetPositionOrder(
        order_id=str(uuid4()),
        signal_id=str(uuid4()),
        symbol="ABC",
        side=side,
        requested_at=requested_at,
        target_fraction=target,
        reference_price=Decimal("100"),
    )


def _candle(timestamp: datetime, *, volume: int = 1000) -> Candle:
    return Candle(
        symbol="ABC",
        interval=CandleInterval.DAY_1,
        timestamp=timestamp,
        open_price=Decimal("100"),
        high_price=Decimal("110"),
        low_price=Decimal("90"),
        close_price=Decimal("105"),
        volume=volume,
        currency="USD",
        source="test",
    )


def test_next_open_execution_applies_costs_and_volume_limit() -> None:
    requested_at = datetime(2026, 1, 1, tzinfo=UTC)
    portfolio = LongOnlyPortfolioLedger("ABC", Decimal("10000"))
    snapshot = portfolio.snapshot(
        timestamp=requested_at + timedelta(days=1),
        market_price=Decimal("100"),
    )
    model = NextOpenExecutionModel(
        BasisPointsCommissionModel(Decimal("100")),
        FixedBasisPointsSlippageModel(Decimal("50")),
        max_volume_participation=Decimal("0.05"),
    )
    decision = model.execute(
        _order(side=OrderSide.BUY, requested_at=requested_at),
        _candle(requested_at + timedelta(days=1), volume=1000),
        snapshot,
    )
    assert decision.fill is not None
    assert decision.fill.quantity == 50
    assert decision.fill.execution_price == Decimal("100.5")
    assert decision.fill.commission == Decimal("50.25")
    assert decision.fill.slippage_cost == Decimal("25.0")
    assert decision.requested_quantity > decision.fill.quantity


def test_execution_rejects_same_candle_fill() -> None:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    portfolio = LongOnlyPortfolioLedger("ABC", Decimal("10000"))
    snapshot = portfolio.snapshot(timestamp=timestamp, market_price=Decimal("100"))
    model = NextOpenExecutionModel(
        BasisPointsCommissionModel(Decimal("0")),
        FixedBasisPointsSlippageModel(Decimal("0")),
        max_volume_participation=Decimal("1"),
    )
    with pytest.raises(BacktestInvariantError, match="strictly after"):
        model.execute(
            _order(side=OrderSide.BUY, requested_at=timestamp),
            _candle(timestamp),
            snapshot,
        )


class _FlatCommissionModel:
    def calculate(self, notional: Decimal) -> Decimal:
        del notional
        return Decimal("5")


def test_generic_commission_sizing_uses_bounded_binary_search() -> None:
    requested_at = datetime(2026, 1, 1, tzinfo=UTC)
    portfolio = LongOnlyPortfolioLedger("ABC", Decimal("105"))
    snapshot = portfolio.snapshot(
        timestamp=requested_at + timedelta(days=1),
        market_price=Decimal("10"),
    )
    model = NextOpenExecutionModel(
        _FlatCommissionModel(),
        FixedBasisPointsSlippageModel(Decimal("0")),
        max_volume_participation=Decimal("1"),
    )
    candle = Candle(
        symbol="ABC",
        interval=CandleInterval.DAY_1,
        timestamp=requested_at + timedelta(days=1),
        open_price=Decimal("10"),
        high_price=Decimal("10"),
        low_price=Decimal("10"),
        close_price=Decimal("10"),
        volume=1_000,
        currency="USD",
        source="test",
    )
    decision = model.execute(
        _order(side=OrderSide.BUY, requested_at=requested_at),
        candle,
        snapshot,
    )
    assert decision.fill is not None
    assert decision.fill.quantity == 10
    assert decision.fill.notional + decision.fill.commission == Decimal("105")
