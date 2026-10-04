from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from world_quant_system.backtest import (
    Fill,
    FlatRateDividendTaxModel,
    LongOnlyPortfolioLedger,
    OrderSide,
)
from world_quant_system.research import (
    CorporateActionEligibilityError,
    CorporateActionPolicy,
    CorporateActionRecord,
    CorporateActionType,
    FractionalSharePolicy,
)

START = datetime(2020, 1, 1, tzinfo=UTC)


def fill(
    *,
    symbol: str,
    side: OrderSide,
    quantity: int,
    price: str,
    at: datetime,
) -> Fill:
    decimal_price = Decimal(price)
    return Fill(
        fill_id=str(uuid4()),
        order_id=str(uuid4()),
        symbol=symbol,
        side=side,
        quantity=quantity,
        reference_price=decimal_price,
        execution_price=decimal_price,
        notional=decimal_price * quantity,
        commission=Decimal("0"),
        slippage_cost=Decimal("0"),
        filled_at=at,
    )


def split(
    *,
    action_type: CorporateActionType,
    numerator: int,
    denominator: int,
    cash_in_lieu_price: Decimal | None = None,
) -> CorporateActionRecord:
    return CorporateActionRecord(
        exchange="XKRX",
        symbol="ABC",
        action_type=action_type,
        effective_at=START + timedelta(days=2),
        available_at=START,
        source="test",
        source_digest="a" * 64,
        ratio_numerator=numerator,
        ratio_denominator=denominator,
        cash_in_lieu_price=cash_in_lieu_price,
    )


def test_two_for_one_split_preserves_equity_and_total_basis() -> None:
    ledger = LongOnlyPortfolioLedger("ABC", Decimal("1000"))
    ledger.apply_fill(
        fill(
            symbol="ABC",
            side=OrderSide.BUY,
            quantity=10,
            price="10",
            at=START,
        )
    )
    before = ledger.snapshot(
        timestamp=START + timedelta(days=1),
        market_price=Decimal("10"),
    )
    action = split(
        action_type=CorporateActionType.SPLIT,
        numerator=2,
        denominator=1,
    )

    application = ledger.apply_corporate_action(
        action.events[0],
        policy=CorporateActionPolicy(),
        dividend_tax_model=FlatRateDividendTaxModel(),
        reference_price=Decimal("10"),
    )
    after = ledger.snapshot(
        timestamp=action.effective_at,
        market_price=Decimal("5"),
    )

    assert before.total_equity == after.total_equity
    assert after.quantity == 20
    assert after.average_cost == Decimal("5")
    assert application.cash_delta == Decimal("0")


def test_reverse_split_fractional_share_can_fail_closed() -> None:
    ledger = LongOnlyPortfolioLedger("ABC", Decimal("1000"))
    ledger.apply_fill(
        fill(
            symbol="ABC",
            side=OrderSide.BUY,
            quantity=3,
            price="10",
            at=START,
        )
    )
    action = split(
        action_type=CorporateActionType.REVERSE_SPLIT,
        numerator=1,
        denominator=2,
        cash_in_lieu_price=Decimal("20"),
    )

    with pytest.raises(CorporateActionEligibilityError):
        ledger.apply_corporate_action(
            action.events[0],
            policy=CorporateActionPolicy(
                fractional_share_policy=FractionalSharePolicy.REJECT
            ),
            dividend_tax_model=FlatRateDividendTaxModel(),
            reference_price=Decimal("10"),
        )


def test_reverse_split_cash_in_lieu_realizes_fractional_value() -> None:
    ledger = LongOnlyPortfolioLedger("ABC", Decimal("1000"))
    ledger.apply_fill(
        fill(
            symbol="ABC",
            side=OrderSide.BUY,
            quantity=3,
            price="10",
            at=START,
        )
    )
    action = split(
        action_type=CorporateActionType.REVERSE_SPLIT,
        numerator=1,
        denominator=2,
        cash_in_lieu_price=Decimal("20"),
    )

    ledger.apply_corporate_action(
        action.events[0],
        policy=CorporateActionPolicy(
            fractional_share_policy=FractionalSharePolicy.CASH_IN_LIEU
        ),
        dividend_tax_model=FlatRateDividendTaxModel(),
        reference_price=Decimal("10"),
    )
    snapshot = ledger.snapshot(
        timestamp=action.effective_at,
        market_price=Decimal("20"),
    )

    assert snapshot.quantity == 1
    assert snapshot.cash == Decimal("980")
    assert snapshot.position_cost_basis == Decimal("20")
    assert snapshot.total_cash_in_lieu == Decimal("10")
    assert snapshot.total_equity == Decimal("1000")


def test_dividend_entitlement_is_fixed_at_ex_date_and_paid_net() -> None:
    ledger = LongOnlyPortfolioLedger("ABC", Decimal("1000"))
    ledger.apply_fill(
        fill(
            symbol="ABC",
            side=OrderSide.BUY,
            quantity=10,
            price="10",
            at=START,
        )
    )
    ex_at = START + timedelta(days=2)
    payment_at = START + timedelta(days=5)
    action = CorporateActionRecord(
        exchange="XKRX",
        symbol="ABC",
        action_type=CorporateActionType.CASH_DIVIDEND,
        effective_at=ex_at,
        available_at=START,
        source="test",
        source_digest="b" * 64,
        cash_amount_per_share=Decimal("1"),
        declared_at=START,
        ex_at=ex_at,
        record_at=START + timedelta(days=3),
        payment_at=payment_at,
    )
    tax_model = FlatRateDividendTaxModel(Decimal("0.15"))

    ledger.apply_corporate_action(
        action.events[0],
        policy=CorporateActionPolicy(),
        dividend_tax_model=tax_model,
        reference_price=Decimal("10"),
    )
    ledger.apply_fill(
        fill(
            symbol="ABC",
            side=OrderSide.SELL,
            quantity=10,
            price="10",
            at=START + timedelta(days=4),
        )
    )
    application = ledger.apply_corporate_action(
        action.events[1],
        policy=CorporateActionPolicy(),
        dividend_tax_model=tax_model,
        reference_price=Decimal("10"),
    )
    snapshot = ledger.snapshot(
        timestamp=payment_at,
        market_price=Decimal("10"),
    )

    assert application.gross_amount == Decimal("10")
    assert application.tax_amount == Decimal("1.50000000")
    assert application.net_amount == Decimal("8.50000000")
    assert snapshot.total_dividend_net == Decimal("8.50000000")
    assert snapshot.cash == Decimal("1008.50000000")


def test_symbol_change_and_zero_value_delisting_preserve_position_identity() -> None:
    ledger = LongOnlyPortfolioLedger("OLD", Decimal("1000"))
    ledger.apply_fill(
        fill(
            symbol="OLD",
            side=OrderSide.BUY,
            quantity=10,
            price="10",
            at=START,
        )
    )
    symbol_change = CorporateActionRecord(
        exchange="XNAS",
        symbol="OLD",
        action_type=CorporateActionType.SYMBOL_CHANGE,
        effective_at=START + timedelta(days=2),
        available_at=START,
        source="test",
        source_digest="c" * 64,
        new_symbol="NEW",
    )
    ledger.apply_corporate_action(
        symbol_change.events[0],
        policy=CorporateActionPolicy(),
        dividend_tax_model=FlatRateDividendTaxModel(),
        reference_price=Decimal("10"),
    )
    assert ledger.symbol == "NEW"

    delisting = CorporateActionRecord(
        exchange="XNAS",
        symbol="NEW",
        action_type=CorporateActionType.DELISTING,
        effective_at=START + timedelta(days=4),
        available_at=START + timedelta(days=3),
        source="test",
        source_digest="d" * 64,
        delisting_cash_price=Decimal("0"),
    )
    ledger.apply_corporate_action(
        delisting.events[0],
        policy=CorporateActionPolicy(),
        dividend_tax_model=FlatRateDividendTaxModel(),
        reference_price=Decimal("8"),
    )
    snapshot = ledger.snapshot(
        timestamp=delisting.effective_at,
        market_price=Decimal("8"),
    )

    assert snapshot.quantity == 0
    assert snapshot.cash == Decimal("900")
    assert snapshot.realized_pnl == Decimal("-100")
    assert ledger.closed_trades[0].symbol == "NEW"


def test_dividend_paid_after_sale_updates_entitled_closed_trade() -> None:
    ledger = LongOnlyPortfolioLedger("ABC", Decimal("1000"))
    ledger.apply_fill(
        fill(
            symbol="ABC",
            side=OrderSide.BUY,
            quantity=10,
            price="10",
            at=START,
        )
    )
    ex_at = START + timedelta(days=2)
    payment_at = START + timedelta(days=5)
    action = CorporateActionRecord(
        exchange="XNAS",
        symbol="ABC",
        action_type=CorporateActionType.CASH_DIVIDEND,
        effective_at=ex_at,
        available_at=START,
        source="test",
        source_digest="e" * 64,
        cash_amount_per_share=Decimal("1"),
        declared_at=START,
        ex_at=ex_at,
        record_at=START + timedelta(days=3),
        payment_at=payment_at,
    )
    tax_model = FlatRateDividendTaxModel(Decimal("0"))

    ledger.apply_corporate_action(
        action.events[0],
        policy=CorporateActionPolicy(),
        dividend_tax_model=tax_model,
        reference_price=Decimal("10"),
    )
    ledger.apply_fill(
        fill(
            symbol="ABC",
            side=OrderSide.SELL,
            quantity=10,
            price="10",
            at=START + timedelta(days=4),
        )
    )
    assert ledger.closed_trades[0].net_pnl == Decimal("0")

    ledger.apply_corporate_action(
        action.events[1],
        policy=CorporateActionPolicy(),
        dividend_tax_model=tax_model,
        reference_price=Decimal("10"),
    )

    trade = ledger.closed_trades[0]
    assert trade.exit_proceeds == Decimal("110")
    assert trade.net_pnl == Decimal("10")
    assert trade.return_fraction == Decimal("0.1")
