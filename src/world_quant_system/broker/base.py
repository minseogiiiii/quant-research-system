from abc import ABC, abstractmethod

from world_quant_system.domain.models import Position, Quote


class MarketDataProvider(ABC):
    @abstractmethod
    async def get_quote(self, symbol: str) -> Quote:
        """Return the latest quote for a symbol."""
        raise NotImplementedError


class PortfolioReader(ABC):
    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Return a snapshot of the current account positions."""
        raise NotImplementedError
