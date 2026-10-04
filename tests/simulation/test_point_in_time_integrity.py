import asyncio

from world_quant_system.research.point_in_time_simulation import (
    run_point_in_time_simulation,
)


def test_point_in_time_integrity_simulation() -> None:
    result = asyncio.run(run_point_in_time_simulation())

    assert result.snapshot_digest_match
    assert result.context_digest_match
    assert result.future_data_blocked
    assert result.overlap_blocked
    assert result.delisted_security_retained
    assert result.member_count == 2
