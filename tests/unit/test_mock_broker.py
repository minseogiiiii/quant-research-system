from datetime import UTC, datetime
from decimal import Decimal

import pytest

from world_quant_system.broker.market_data import MarketDataProvider
from world_quant_system.broker.mock import MockBroker
from world_quant_system.broker.portfolio import PortfolioReader
from world_quant_system.domain.models import Position, Quote


def make_quote(symbol: str, price: str) -> Quote:
    return Quote(
        symbol=symbol,
        price=Decimal(price),
        timestamp=datetime.now(UTC),
        source="test",
    )


def test_mock_broker_implements_read_interfaces() -> None:
    assert issubclass(MockBroker, MarketDataProvider)
    assert issubclass(MockBroker, PortfolioReader)


@pytest.mark.asyncio
async def test_mock_broker_returns_quote() -> None:
    quote = make_quote("005930", "95000")
    broker = MockBroker(quotes={"005930": quote})

    result = await broker.get_quote("005930")

    assert result == quote


@pytest.mark.asyncio
async def test_mock_broker_returns_multiple_quotes() -> None:
    samsung = make_quote("005930", "95000")
    sk_hynix = make_quote("000660", "210000")

    broker = MockBroker(
        quotes={
            "005930": samsung,
            "000660": sk_hynix,
        }
    )

    results = await broker.get_quotes(
        ["005930", "000660"]
    )

    assert results == [samsung, sk_hynix]


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

    with pytest.raises(
        LookupError,
        match="No quote is available",
    ):
        await broker.get_quote("UNKNOWN")
