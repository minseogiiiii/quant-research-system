from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

from world_quant_system.broker.market_data import (
    CandleDataProvider,
    MarketDataProvider,
)
from world_quant_system.broker.portfolio import PortfolioReader
from world_quant_system.domain.models import (
    CandleInterval,
    CandlePage,
    Position,
    Quote,
)


class MockBroker(
    MarketDataProvider,
    CandleDataProvider,
    PortfolioReader,
):
    def __init__(
        self,
        quotes: Mapping[str, Quote] | None = None,
        positions: Iterable[Position] | None = None,
        candle_pages: Mapping[tuple[str, CandleInterval], CandlePage] | None = None,
    ) -> None:
        self._quotes = dict(quotes or {})
        self._positions = tuple(positions or ())
        self._candle_pages = dict(candle_pages or {})

    async def get_quote(self, symbol: str) -> Quote:
        try:
            return self._quotes[symbol]
        except KeyError as error:
            raise LookupError(f"No quote is available for symbol: {symbol}") from error

    async def get_quotes(
        self,
        symbols: Sequence[str],
    ) -> list[Quote]:
        return [await self.get_quote(symbol) for symbol in symbols]

    async def get_candles(
        self,
        symbol: str,
        interval: CandleInterval,
        *,
        count: int = 100,
        before: datetime | None = None,
        adjusted: bool = True,
    ) -> CandlePage:
        del count, before, adjusted

        try:
            return self._candle_pages[(symbol, interval)]
        except KeyError as error:
            raise LookupError(
                "No candle page is available for "
                f"symbol={symbol}, interval={interval.value}"
            ) from error

    async def get_positions(self) -> list[Position]:
        return list(self._positions)
