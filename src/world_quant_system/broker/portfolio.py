from abc import ABC, abstractmethod

from world_quant_system.domain.models import Position


class PortfolioReader(ABC):
    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Return a snapshot of current account positions."""
        raise NotImplementedError
