import pytest

from world_quant_system.research.historical_dataset_simulation import (
    run_historical_dataset_simulation,
)


@pytest.mark.asyncio
async def test_historical_dataset_simulation() -> None:
    result = await run_historical_dataset_simulation()
    assert result["items"] == 3
    assert result["idempotent_import"] is True
    assert result["deterministic_cross_catalog_digest"] is True
    assert result["frozen"] is True
    assert result["network_used"] is False
