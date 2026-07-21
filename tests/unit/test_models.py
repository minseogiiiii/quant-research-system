from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from world_quant_system.domain.models import (
    Candle,
    CandleInterval,
    CandlePage,
    Position,
    Quote,
)

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def make_candle(
    *,
    timestamp: datetime = NOW,
    symbol: str = "005930",
    currency: str = "KRW",
    source: str = "test",
) -> Candle:
    return Candle(
        symbol=symbol,
        interval=CandleInterval.DAY_1,
        timestamp=timestamp,
        open_price=Decimal("71000"),
        high_price=Decimal("73000"),
        low_price=Decimal("70000"),
        close_price=Decimal("72000"),
        volume=1_000_000,
        currency=currency,
        source=source,
    )


def test_quote_accepts_valid_financial_data() -> None:
    quote = Quote(
        symbol="005930",
        price=Decimal("95000"),
        timestamp=NOW,
        currency="KRW",
        source="mock",
    )

    assert quote.symbol == "005930"
    assert quote.price == Decimal("95000")
    assert quote.currency == "KRW"


@pytest.mark.parametrize("symbol", ["", "   "])
def test_quote_rejects_blank_symbol(symbol: str) -> None:
    with pytest.raises(ValueError, match="Symbol cannot be empty"):
        Quote(
            symbol=symbol,
            price=Decimal("95000"),
            timestamp=NOW,
            currency="KRW",
            source="mock",
        )


@pytest.mark.parametrize("price", [Decimal("0"), Decimal("-1")])
def test_quote_rejects_nonpositive_price(price: Decimal) -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        Quote(
            symbol="005930",
            price=price,
            timestamp=NOW,
            currency="KRW",
            source="mock",
        )


@pytest.mark.parametrize("price", [Decimal("NaN"), Decimal("Infinity")])
def test_quote_rejects_nonfinite_price(price: Decimal) -> None:
    with pytest.raises(ValueError, match="finite"):
        Quote(
            symbol="005930",
            price=price,
            timestamp=NOW,
            currency="KRW",
            source="mock",
        )


def test_quote_requires_timezone_aware_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone"):
        Quote(
            symbol="005930",
            price=Decimal("95000"),
            timestamp=datetime.now(),
            currency="KRW",
            source="mock",
        )


@pytest.mark.parametrize("currency", ["", "KR", "KR1", "원화"])
def test_quote_rejects_invalid_currency(currency: str) -> None:
    with pytest.raises(ValueError, match="Currency"):
        Quote(
            symbol="005930",
            price=Decimal("95000"),
            timestamp=NOW,
            currency=currency,
            source="mock",
        )


def test_candle_accepts_valid_ohlcv_data() -> None:
    candle = make_candle()

    assert candle.interval is CandleInterval.DAY_1
    assert candle.close_price == Decimal("72000")
    assert candle.volume == 1_000_000


def test_candle_rejects_high_below_low() -> None:
    with pytest.raises(ValueError, match="High price"):
        Candle(
            symbol="005930",
            interval=CandleInterval.DAY_1,
            timestamp=NOW,
            open_price=Decimal("71000"),
            high_price=Decimal("70000"),
            low_price=Decimal("72000"),
            close_price=Decimal("71000"),
            volume=1,
            currency="KRW",
            source="test",
        )


@pytest.mark.parametrize(
    ("open_price", "close_price", "message"),
    [
        (Decimal("69000"), Decimal("72000"), "Open price"),
        (Decimal("71000"), Decimal("74000"), "Close price"),
    ],
)
def test_candle_rejects_price_outside_range(
    open_price: Decimal,
    close_price: Decimal,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        Candle(
            symbol="005930",
            interval=CandleInterval.DAY_1,
            timestamp=NOW,
            open_price=open_price,
            high_price=Decimal("73000"),
            low_price=Decimal("70000"),
            close_price=close_price,
            volume=1,
            currency="KRW",
            source="test",
        )


@pytest.mark.parametrize("volume", [-1, True])
def test_candle_rejects_invalid_volume(volume: int) -> None:
    with pytest.raises(ValueError, match="Volume"):
        Candle(
            symbol="005930",
            interval=CandleInterval.DAY_1,
            timestamp=NOW,
            open_price=Decimal("71000"),
            high_price=Decimal("73000"),
            low_price=Decimal("70000"),
            close_price=Decimal("72000"),
            volume=volume,
            currency="KRW",
            source="test",
        )


def test_candle_page_requires_oldest_to_newest_order() -> None:
    newer = make_candle(timestamp=NOW)
    older = make_candle(timestamp=NOW - timedelta(days=1))

    with pytest.raises(ValueError, match="oldest to newest"):
        CandlePage(
            candles=(newer, older),
            next_before=older.timestamp,
        )


def test_candle_page_rejects_mixed_currency() -> None:
    older = make_candle(timestamp=NOW - timedelta(days=1))
    newer = make_candle(timestamp=NOW, currency="USD")

    with pytest.raises(ValueError, match="currencies"):
        CandlePage(
            candles=(older, newer),
            next_before=older.timestamp,
        )


def test_position_rejects_negative_quantity() -> None:
    with pytest.raises(ValueError, match="nonnegative integer"):
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
