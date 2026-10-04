from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from world_quant_system.research.historical_shadow import (
    DeterministicHistoricalShadowRunner,
)
from world_quant_system.research.historical_shadow_models import (
    HistoricalShadowPolicy,
    ShadowEvidenceEvent,
    ShadowEvidenceManifest,
    ShadowEvidenceState,
    ShadowRunDecision,
    ShadowScenarioName,
)
from world_quant_system.research.portfolio_promotion_models import (
    AllocationMethod,
    CandidateReturnMatrix,
)


def build_synthetic_run() -> tuple[
    ShadowEvidenceManifest,
    CandidateReturnMatrix,
    HistoricalShadowPolicy,
]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    timestamps = tuple(start + timedelta(days=index) for index in range(90))
    candidate_ids = ("carry", "quality", "trend")
    returns = (
        tuple(
            Decimal("0.0015") if index % 7 != 0 else Decimal("-0.002")
            for index in range(90)
        ),
        tuple(
            Decimal("0.0012") if index % 9 != 0 else Decimal("-0.0015")
            for index in range(90)
        ),
        tuple(
            Decimal("0.0018") if index % 11 != 0 else Decimal("-0.003")
            for index in range(90)
        ),
    )
    matrix = CandidateReturnMatrix(
        timestamps=timestamps,
        candidate_ids=candidate_ids,
        returns_by_candidate=returns,
    )
    dataset_digest = "a" * 64
    events = tuple(
        ShadowEvidenceEvent(
            candidate_id=candidate_id,
            strategy_name=f"synthetic-{candidate_id}",
            strategy_version="1",
            state=ShadowEvidenceState.PROMOTED,
            effective_at=timestamps[5],
            available_at=timestamps[5],
            evidence_digest=character * 64,
            reason="synthetic promotion evidence",
        )
        for candidate_id, character in zip(
            candidate_ids,
            ("b", "c", "d"),
            strict=True,
        )
    )
    manifest = ShadowEvidenceManifest(
        dataset_digest=dataset_digest,
        return_matrix_digest=matrix.matrix_digest,
        events=events,
    )
    policy = HistoricalShadowPolicy(
        initial_equity=Decimal("1000000"),
        allocation_method=AllocationMethod.CAPPED_INVERSE_VOLATILITY,
        rebalance_frequency=5,
        volatility_lookback=10,
        execution_delay_periods=1,
        minimum_active_candidates=2,
        maximum_candidate_weight=Decimal("0.40"),
        transaction_cost_bps=Decimal("5"),
        maximum_evidence_age_days=365,
        minimum_observations=60,
        minimum_base_cumulative_return=Decimal("0"),
        minimum_stress_cumulative_return=Decimal("-0.02"),
        maximum_stress_drawdown=Decimal("-0.20"),
    )
    return manifest, matrix, policy


def main() -> None:
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
    if first.report_id != second.report_id:
        raise RuntimeError("Historical shadow report ID is not deterministic.")
    if first.report_digest != second.report_digest:
        raise RuntimeError("Historical shadow report digest is not deterministic.")
    if first.ledger[5].weights:
        raise RuntimeError("Decision-period weights executed on the same period.")
    if not first.ledger[6].weights:
        raise RuntimeError("Next-period allocation did not execute.")
    scenarios = {item.scenario: item for item in first.scenarios}
    if (
        scenarios[ShadowScenarioName.DOUBLE_COST].metrics.final_equity
        > scenarios[ShadowScenarioName.BASE].metrics.final_equity
    ):
        raise RuntimeError("Higher costs increased final equity.")
    if first.decision is not ShadowRunDecision.READY_FOR_FORWARD_SHADOW_RESEARCH:
        raise RuntimeError(
            "Synthetic historical shadow run did not pass its research policy."
        )
    print("Execution mode: HISTORICAL_SHADOW")
    print("Network access: DISABLED")
    print("Broker provider: NONE")
    print("Live trading: DISABLED")
    print("Order submission: DISABLED")
    print(f"Report ID: {first.report_id}")
    print(f"Report digest: {first.report_digest}")
    print(f"Decision: {first.decision.value}")
    print("Historical shadow deterministic simulation passed.")


if __name__ == "__main__":
    main()
