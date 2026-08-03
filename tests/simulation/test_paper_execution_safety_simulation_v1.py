from world_quant_system.paper_execution.models import (
    KillSwitchMode,
    PaperOrderStatus,
)
from world_quant_system.paper_execution.simulation import (
    run_paper_execution_simulation,
)


def test_paper_execution_simulation_is_deterministic_and_fail_closed() -> None:
    first = run_paper_execution_simulation()
    second = run_paper_execution_simulation()
    assert first == second
    assert first.first_status == PaperOrderStatus.SUBMITTED
    assert first.duplicate_status == PaperOrderStatus.SUBMITTED
    assert first.broker_submit_calls == 2
    assert first.reconciliation_passed
    assert first.timeout_recovered
    assert first.kill_switch_after_timeout == KillSwitchMode.SOFT_HALT
    assert first.live_trading_enabled is False
