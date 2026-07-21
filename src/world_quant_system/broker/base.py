from world_quant_system.broker.market_data import MarketDataProvider
from world_quant_system.broker.portfolio import PortfolioReader


class Broker(
    MarketDataProvider,
    PortfolioReader,
):
    """Temporary compatibility interface.

    New adapters should implement MarketDataProvider
    and PortfolioReader according to their capabilities.
    """
