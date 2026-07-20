import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from world_quant_system.broker.mock import MockBroker
from world_quant_system.config.settings import Settings
from world_quant_system.domain.models import Position, Quote


async def build_demo_report() -> str:
    settings = Settings()

    quote = Quote(
        symbol="005930",
        price=Decimal("95000"),
        timestamp=datetime.now(UTC),
        source="mock",
    )

    position = Position(
        symbol="005930",
        quantity=3,
        average_price=Decimal("90000"),
    )

    broker = MockBroker(
        quotes={"005930": quote},
        positions=[position],
    )

    latest_quote = await broker.get_quote("005930")
    positions = await broker.get_positions()

    position_lines = [
        (
            f"{item.symbol} | "
            f"quantity={item.quantity} | "
            f"average_price={item.average_price:,.0f} KRW"
        )
        for item in positions
    ]

    return "\n".join(
        [
            f"Environment: {settings.kis_env.value.upper()}",
            "Live trading: DISABLED",
            "",
            "Samsung Electronics",
            f"Symbol: {latest_quote.symbol}",
            f"Price: {latest_quote.price:,.0f} KRW",
            f"Source: {latest_quote.source}",
            "",
            "Account positions:",
            *position_lines,
            "",
            "Order submission:",
            "DISABLED",
        ]
    )


def main() -> None:
    report = asyncio.run(build_demo_report())
    print(report)