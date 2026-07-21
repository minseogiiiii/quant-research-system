from datetime import UTC, datetime
from decimal import Decimal

import pytest

from world_quant_system.domain.models import Position, Quote


def test_quote_accepts_valid_financial_data() -> None:
    quote = Quote(
        symbol="005930",
        price=Decimal("95000"),
        timestamp=datetime.now(UTC),
        source="mock",
    )

    assert quote.symbol == "005930"
    assert quote.price == Decimal("95000")


@pytest.mark.parametrize("symbol", ["", "   "])
def test_quote_rejects_blank_symbol(symbol: str) -> None:
    with pytest.raises(ValueError, match="Symbol cannot be empty"):
        Quote(
            symbol=symbol,
            price=Decimal("95000"),
            timestamp=datetime.now(UTC),
            source="mock",
        )


def test_quote_rejects_nonpositive_price() -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        Quote(
            symbol="005930",
            price=Decimal("0"),
            timestamp=datetime.now(UTC),
            source="mock",
        )


def test_quote_requires_timezone_aware_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone"):
        Quote(
            symbol="005930",
            price=Decimal("95000"),
            timestamp=datetime.now(),
            source="mock",
        )


def test_position_rejects_negative_quantity() -> None:
    with pytest.raises(ValueError, match="Quantity cannot be negative"):
        Position(
            symbol="005930",
            quantity=-1,
            average_price=Decimal("90000"),
        )


def test_position_rejects_negative_average_price() -> None:
    with pytest.raises(ValueError, match="Average price cannot be negative"):
        Position(
            symbol="005930",
            quantity=1,
            average_price=Decimal("-1"),
        )
