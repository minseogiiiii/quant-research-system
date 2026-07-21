from abc import ABC, abstractmethod
from collections.abc import Sequence

from world_quant_system.domain.models import Quote


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
