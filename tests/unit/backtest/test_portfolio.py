from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from world_quant_system.backtest import Fill, LongOnlyPortfolioLedger, OrderSide
from world_quant_system.backtest.models import BacktestInvariantError


def _fill(
    *,
    side: OrderSide,
    quantity: int,
    price: Decimal,
    commission: Decimal,
    timestamp: datetime,
) -> Fill:
    return Fill(
        fill_id=str(uuid4()),
        order_id=str(uuid4()),
        symbol="ABC",
        side=side,
        quantity=quantity,
        reference_price=price,
        execution_price=price,
        notional=price * quantity,
        commission=commission,
        slippage_cost=Decimal("0"),
        filled_at=timestamp,
    )


def test_portfolio_accounting_matches_manual_long_trade() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    ledger = LongOnlyPortfolioLedger("ABC", Decimal("1000"))
    buy = _fill(
        side=OrderSide.BUY,
        quantity=99,
        price=Decimal("10"),
        commission=Decimal("9.9"),
        timestamp=start,
    )
    ledger.apply_fill(buy)
    after_buy = ledger.snapshot(
        timestamp=start,
        market_price=Decimal("10"),
    )
    assert after_buy.cash == Decimal("0.1")
    assert after_buy.position_cost_basis == Decimal("999.9")
    assert after_buy.total_equity == Decimal("990.1")

    sell = _fill(
        side=OrderSide.SELL,
        quantity=99,
        price=Decimal("12"),
        commission=Decimal("11.88"),
        timestamp=start + timedelta(days=1),
    )
    ledger.apply_fill(sell)
    final = ledger.snapshot(
        timestamp=start + timedelta(days=1),
        market_price=Decimal("12"),
    )
    assert final.cash == Decimal("1176.22")
    assert final.quantity == 0
    assert final.realized_pnl == Decimal("176.22")
    assert ledger.closed_trades[0].net_pnl == Decimal("176.22")


def test_duplicate_fill_is_blocked() -> None:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    ledger = LongOnlyPortfolioLedger("ABC", Decimal("1000"))
    fill = _fill(
        side=OrderSide.BUY,
        quantity=10,
        price=Decimal("10"),
        commission=Decimal("0"),
        timestamp=timestamp,
    )
    ledger.apply_fill(fill)
    with pytest.raises(BacktestInvariantError, match="same fill"):
        ledger.apply_fill(fill)
