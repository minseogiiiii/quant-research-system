from datetime import UTC, datetime
from decimal import Decimal

import pytest

from world_quant_system.broker.mock import MockBroker
from world_quant_system.domain.models import Position, Quote


@pytest.mark.asyncio
async def test_mock_broker_returns_quote() -> None:
    quote = Quote(
        symbol="005930",
        price=Decimal("95000"),
        timestamp=datetime.now(UTC),
        source="test",
    )

    broker = MockBroker(
        quotes={
            "005930": quote,
        }
    )

    result = await broker.get_quote("005930")

    assert result == quote
    assert result.symbol == "005930"
    assert result.price == Decimal("95000")


@pytest.mark.asyncio
async def test_mock_broker_returns_positions() -> None:
    position = Position(
        symbol="005930",
        quantity=3,
        average_price=Decimal("90000"),
    )

    broker = MockBroker(positions=[position])

    positions = await broker.get_positions()

    assert positions == [position]


@pytest.mark.asyncio
async def test_mock_broker_rejects_unknown_symbol() -> None:
    broker = MockBroker()

    with pytest.raises(LookupError):
        await broker.get_quote("UNKNOWN")