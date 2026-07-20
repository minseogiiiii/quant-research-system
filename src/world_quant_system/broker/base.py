from abc import ABC, abstractmethod

from world_quant_system.domain.models import Position, Quote


class Broker(ABC):

    @abstractmethod
    async def get_quote(self, symbol: str) -> Quote:
        """Return the latest quote for a symbol."""
        raise NotImplementedError

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Return the current account positions."""
        raise NotImplementedError