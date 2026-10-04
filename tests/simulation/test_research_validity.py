import pytest

from world_quant_system.research.simulation import run_research_validity_simulation


@pytest.mark.asyncio
async def test_research_validity_simulation() -> None:
    result = await run_research_validity_simulation()

    assert result.experiment_count == 1
    assert result.deterministic_digest_match
    assert result.idempotent_registration
    assert result.failed_outcome_retained
    assert result.holdout_reuse_blocked
