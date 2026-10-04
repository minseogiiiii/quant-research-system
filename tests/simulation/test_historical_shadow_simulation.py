from datetime import UTC, datetime

from world_quant_system.research.historical_shadow import (
    DeterministicHistoricalShadowRunner,
)
from world_quant_system.research.historical_shadow_models import (
    ShadowRunDecision,
)
from world_quant_system.research.historical_shadow_simulation import (
    build_synthetic_run,
)


def test_historical_shadow_simulation_is_deterministic() -> None:
    manifest, matrix, policy = build_synthetic_run()
    first = DeterministicHistoricalShadowRunner(
        evidence_manifest=manifest,
        return_matrix=matrix,
        policy=policy,
    ).run(created_at=datetime(2026, 1, 1, tzinfo=UTC))
    second = DeterministicHistoricalShadowRunner(
        evidence_manifest=manifest,
        return_matrix=matrix,
        policy=policy,
    ).run(created_at=datetime(2026, 2, 1, tzinfo=UTC))

    assert first.report_id == second.report_id
    assert first.report_digest == second.report_digest
    assert first.decision is ShadowRunDecision.READY_FOR_FORWARD_SHADOW_RESEARCH
