from datetime import UTC, datetime
from decimal import Decimal

import pytest

from world_quant_system.broker.market_data import (
    CandleDataProvider,
    MarketDataProvider,
)
from world_quant_system.broker.mock import MockBroker
from world_quant_system.broker.portfolio import PortfolioReader
from world_quant_system.domain.models import (
    Candle,
    CandleInterval,
    CandlePage,
    Position,
    Quote,
)


def make_quote(symbol: str, price: str) -> Quote:
    return Quote(
        symbol=symbol,
        price=Decimal(price),
        timestamp=datetime.now(UTC),
        currency="KRW",
        source="test",
    )


def make_candle_page(symbol: str) -> CandlePage:
    candle = Candle(
        symbol=symbol,
        interval=CandleInterval.DAY_1,
        timestamp=datetime.now(UTC),
        open_price=Decimal("90000"),
        high_price=Decimal("96000"),
        low_price=Decimal("89000"),
        close_price=Decimal("95000"),
        volume=1000,
        currency="KRW",
        source="test",
    )
    return CandlePage(candles=(candle,), next_before=candle.timestamp)


def test_mock_broker_implements_read_interfaces() -> None:
    assert issubclass(MockBroker, MarketDataProvider)
    assert issubclass(MockBroker, CandleDataProvider)
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

    results = await broker.get_quotes(["005930", "000660"])

    assert results == [samsung, sk_hynix]


@pytest.mark.asyncio
async def test_mock_broker_returns_candle_page() -> None:
    page = make_candle_page("005930")
    broker = MockBroker(candle_pages={("005930", CandleInterval.DAY_1): page})

    result = await broker.get_candles(
        "005930",
        CandleInterval.DAY_1,
    )

    assert result == page


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
