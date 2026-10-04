import asyncio
from decimal import Decimal

from world_quant_system.research.corporate_action_simulation import (
    run_corporate_action_simulation,
)


def test_corporate_action_economics_simulation() -> None:
    result = asyncio.run(run_corporate_action_simulation())

    assert result.action_count == 4
    assert result.application_count == 5
    assert result.deterministic_context_digest
    assert result.deterministic_run_digest
    assert result.idempotent_writes
    assert result.conflict_blocked
    assert result.future_action_blocked
    assert result.split_equity_preserved
    assert result.dividend_net == Decimal("90.00000000")
    assert result.delisting_loss_applied
    assert result.final_equity == Decimal("90.00000000")
