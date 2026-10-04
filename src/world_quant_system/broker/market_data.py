from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime

from world_quant_system.domain.models import (
    CandleInterval,
    CandlePage,
    Quote,
)


class MarketDataProvider(ABC):
    @abstractmethod
    async def get_quote(self, symbol: str) -> Quote:
        """Return the latest quote for one symbol."""
        raise NotImplementedError

    @abstractmethod
    async def get_quotes(
        self,
        symbols: Sequence[str],
    ) -> list[Quote]:
        """Return the latest quotes for multiple symbols."""
        raise NotImplementedError


class CandleDataProvider(ABC):
    @abstractmethod
    async def get_candles(
        self,
        symbol: str,
        interval: CandleInterval,
        *,
        count: int = 100,
        before: datetime | None = None,
        adjusted: bool = True,
    ) -> CandlePage:
        """Return one validated page of historical candles."""
        raise NotImplementedError
