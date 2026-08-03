from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from world_quant_system.research.forward_shadow import (
    DeterministicForwardShadowRunner,
    FixedForwardShadowClock,
)
from world_quant_system.research.forward_shadow_models import (
    ForwardShadowObservation,
    ForwardShadowPolicy,
    ForwardShadowReturn,
)
from world_quant_system.research.historical_shadow_models import (
    ShadowEvidenceEvent,
    ShadowEvidenceManifest,
    ShadowEvidenceState,
)
from world_quant_system.research.portfolio_promotion_models import (
    AllocationMethod,
)


def build_synthetic_forward_shadow() -> tuple[
    ShadowEvidenceManifest,
    ForwardShadowPolicy,
    tuple[ForwardShadowObservation, ...],
    datetime,
]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candidate_ids = ("carry", "quality", "trend")
    manifest = ShadowEvidenceManifest(
        dataset_digest="a" * 64,
        return_matrix_digest="f" * 64,
        events=tuple(
            ShadowEvidenceEvent(
                candidate_id=candidate_id,
                strategy_name=f"synthetic-{candidate_id}",
                strategy_version="1",
                state=ShadowEvidenceState.PROMOTED,
                effective_at=start,
                available_at=start,
                evidence_digest=character * 64,
                reason="synthetic forward-shadow promotion evidence",
            )
            for candidate_id, character in zip(
                candidate_ids,
                ("b", "c", "d"),
                strict=True,
            )
        ),
    )
    policy = ForwardShadowPolicy(
        initial_equity=Decimal("1000000"),
        allocation_method=AllocationMethod.CAPPED_INVERSE_VOLATILITY,
        rebalance_every_observations=2,
        volatility_lookback=4,
        execution_delay_observations=1,
        minimum_active_candidates=2,
        maximum_candidate_weight=Decimal("0.40"),
        transaction_cost_bps=Decimal("5"),
        maximum_evidence_age_days=365,
        maximum_observation_lateness_seconds=60,
        maximum_future_clock_skew_seconds=0,
        maximum_drawdown_before_halt=Decimal("-0.25"),
    )
    observations = tuple(
        ForwardShadowObservation(
            sequence=index,
            observed_at=start + timedelta(days=index),
            received_at=start + timedelta(days=index, seconds=1),
            returns=tuple(
                ForwardShadowReturn(
                    candidate_id=candidate_id,
                    value=_synthetic_return(candidate_id, index),
                )
                for candidate_id in candidate_ids
            ),
            source_digest=f"{index + 1:064x}",
        )
        for index in range(12)
    )
    now = observations[-1].received_at
    return manifest, policy, observations, now


def main() -> None:
    manifest, policy, observations, now = build_synthetic_forward_shadow()
    runner = DeterministicForwardShadowRunner(
        evidence_manifest=manifest,
        policy=policy,
        clock=FixedForwardShadowClock(now),
    )
    full = runner.process(
        observations,
        generated_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    repeated_full = runner.process(
        observations,
        generated_at=datetime(2026, 4, 1, tzinfo=UTC),
    )
    partial = runner.process(observations[:6])
    resumed = runner.process(
        observations[6:],
        state=partial.state,
        generated_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    if full.state.state_digest != resumed.state.state_digest:
        raise RuntimeError("Resumed state differs from one-shot processing.")
    if full.report_id != repeated_full.report_id:
        raise RuntimeError("Forward-shadow report identity is not deterministic.")
    if full.state.ledger[0].weights:
        raise RuntimeError("Decision-period allocation executed immediately.")
    if not full.state.ledger[1].weights:
        raise RuntimeError("Next-observation allocation did not execute.")
    duplicate = runner.process(observations[-2:], state=full.state)
    if duplicate.state.state_digest != full.state.state_digest:
        raise RuntimeError("Idempotent replay changed checkpoint state.")
    if duplicate.duplicate_observations != 2:
        raise RuntimeError("Idempotent replay was not counted correctly.")
    print("Execution mode: FORWARD_SHADOW")
    print("Network access: DISABLED")
    print("Broker provider: NONE")
    print("Live trading: DISABLED")
    print("Order submission: DISABLED")
    print(f"State digest: {full.state.state_digest}")
    print(f"Report digest: {full.report_digest}")
    print(f"Decision: {full.state.decision.value}")
    print("Forward shadow deterministic simulation passed.")


def _synthetic_return(candidate_id: str, index: int) -> Decimal:
    patterns = {
        "carry": (Decimal("0.0015"), Decimal("-0.0010"), 5),
        "quality": (Decimal("0.0012"), Decimal("-0.0008"), 7),
        "trend": (Decimal("0.0018"), Decimal("-0.0015"), 4),
    }
    positive, negative, period = patterns[candidate_id]
    return negative if index % period == 0 else positive


if __name__ == "__main__":
    main()
