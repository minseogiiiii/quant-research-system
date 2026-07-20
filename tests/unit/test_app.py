import pytest

from world_quant_system.app import build_demo_report


@pytest.mark.asyncio
async def test_build_demo_report() -> None:
    report = await build_demo_report()

    assert "Environment: PAPER" in report
    assert "Live trading: DISABLED" in report
    assert "Symbol: 005930" in report
    assert "Price: 95,000 KRW" in report
    assert "quantity=3" in report
    assert "Order submission:" in report
    assert report.endswith("DISABLED")