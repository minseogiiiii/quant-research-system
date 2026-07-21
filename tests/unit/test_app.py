import pytest

from world_quant_system.app import build_demo_report
from world_quant_system.config.settings import (
    BrokerProvider,
    ExecutionMode,
    Settings,
)


@pytest.mark.asyncio
async def test_build_demo_report() -> None:
    settings = Settings(
        execution_mode=ExecutionMode.MOCK,
        broker_provider=BrokerProvider.NONE,
        live_trading_enabled=False,
    )

    report = await build_demo_report(settings)

    assert "Execution mode: MOCK" in report
    assert "Broker provider: NONE" in report
    assert "Live trading: DISABLED" in report
    assert "Symbol: 005930" in report
    assert "Price: 95,000 KRW" in report
    assert "quantity=3" in report
    assert "Order submission:" in report
    assert report.endswith("DISABLED")