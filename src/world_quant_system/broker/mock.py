from collections.abc import Iterable, Mapping

from world_quant_system.broker.base import Broker
from world_quant_system.domain.models import Position, Quote


class MockBroker(Broker):

    def __init__(
        self,
        quotes: Mapping[str, Quote] | None = None,
        positions: Iterable[Position] | None = None,
    ) -> None:
        self._quotes = dict(quotes or {})
        self._positions = list(positions or [])

    async def get_quote(self, symbol: str) -> Quote:
        try:
            return self._quotes[symbol]
        except KeyError as error:
            raise LookupError(
                f"No quote is available for symbol: {symbol}"
            ) from error

    async def get_positions(self) -> list[Position]:
        return list(self._positions)