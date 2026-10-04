from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from world_quant_system.research.forward_shadow import (
    DeterministicForwardShadowRunner,
    FixedForwardShadowClock,
)
from world_quant_system.research.forward_shadow_models import (
    ForwardShadowIntegrityError,
    ForwardShadowObservation,
    ForwardShadowReturn,
)
from world_quant_system.research.forward_shadow_simulation import (
    build_synthetic_forward_shadow,
)
from world_quant_system.research.historical_shadow_models import (
    ShadowEvidenceEvent,
    ShadowEvidenceManifest,
    ShadowEvidenceState,
)


def test_allocation_executes_on_next_observation() -> None:
    manifest, policy, observations, now = build_synthetic_forward_shadow()
    report = DeterministicForwardShadowRunner(
        evidence_manifest=manifest,
        policy=policy,
        clock=FixedForwardShadowClock(now),
    ).process(observations[:3])

    assert report.state.ledger[0].weights == ()
    assert report.state.ledger[1].weights


def test_resumed_processing_matches_one_shot_state() -> None:
    manifest, policy, observations, now = build_synthetic_forward_shadow()
    runner = DeterministicForwardShadowRunner(
        evidence_manifest=manifest,
        policy=policy,
        clock=FixedForwardShadowClock(now),
    )
    full = runner.process(observations)
    partial = runner.process(observations[:5])
    resumed = runner.process(observations[5:], state=partial.state)

    assert resumed.state.state_digest == full.state.state_digest


def test_duplicate_observations_are_idempotent() -> None:
    manifest, policy, observations, now = build_synthetic_forward_shadow()
    runner = DeterministicForwardShadowRunner(
        evidence_manifest=manifest,
        policy=policy,
        clock=FixedForwardShadowClock(now),
    )
    first = runner.process(observations[:4])
    duplicate = runner.process(observations[2:4], state=first.state)

    assert duplicate.processed_observations == 0
    assert duplicate.duplicate_observations == 2
    assert duplicate.state.state_digest == first.state.state_digest


def test_sequence_gap_is_rejected() -> None:
    manifest, policy, observations, now = build_synthetic_forward_shadow()
    runner = DeterministicForwardShadowRunner(
        evidence_manifest=manifest,
        policy=policy,
        clock=FixedForwardShadowClock(now),
    )
    first = runner.process(observations[:1])

    with pytest.raises(ForwardShadowIntegrityError, match="sequence"):
        runner.process(observations[2:3], state=first.state)


def test_future_observation_is_rejected() -> None:
    manifest, policy, observations, now = build_synthetic_forward_shadow()
    future = replace(
        observations[0],
        observed_at=now + timedelta(seconds=1),
        received_at=now + timedelta(seconds=1),
    )
    runner = DeterministicForwardShadowRunner(
        evidence_manifest=manifest,
        policy=policy,
        clock=FixedForwardShadowClock(now),
    )

    with pytest.raises(ForwardShadowIntegrityError, match="future"):
        runner.process((future,))


def test_stale_feed_fails_closed_to_cash() -> None:
    manifest, policy, observations, now = build_synthetic_forward_shadow()
    stale = replace(
        observations[0],
        received_at=(
            observations[0].observed_at
            + timedelta(
                seconds=policy.maximum_observation_lateness_seconds + 1
            )
        ),
    )
    runner = DeterministicForwardShadowRunner(
        evidence_manifest=manifest,
        policy=policy,
        clock=FixedForwardShadowClock(now),
    )
    report = runner.process((stale,))

    assert report.state.feed_stale is True
    assert report.state.cash_weight == Decimal("1")
    assert report.state.active_weights == ()


def test_drawdown_halt_fails_closed_to_cash() -> None:
    manifest, policy, observations, now = build_synthetic_forward_shadow()
    strict_policy = replace(
        policy,
        maximum_drawdown_before_halt=Decimal("-0.01"),
    )
    runner = DeterministicForwardShadowRunner(
        evidence_manifest=manifest,
        policy=strict_policy,
        clock=FixedForwardShadowClock(now),
    )
    first = runner.process(observations[:2])
    loss = ForwardShadowObservation(
        sequence=2,
        observed_at=observations[2].observed_at,
        received_at=observations[2].received_at,
        returns=tuple(
            ForwardShadowReturn(item.candidate_id, Decimal("-0.10"))
            for item in observations[2].returns
        ),
        source_digest="e" * 64,
    )
    halted = runner.process((loss,), state=first.state)

    assert halted.state.risk_halted is True
    assert halted.state.cash_weight == Decimal("1")
    assert halted.state.active_weights == ()


def test_append_only_evidence_manifest_extension_is_accepted() -> None:
    manifest, policy, observations, now = build_synthetic_forward_shadow()
    first_runner = DeterministicForwardShadowRunner(
        evidence_manifest=manifest,
        policy=policy,
        clock=FixedForwardShadowClock(now),
    )
    first = first_runner.process(observations[:3])
    suspension = ShadowEvidenceEvent(
        candidate_id="trend",
        strategy_name="synthetic-trend",
        strategy_version="1",
        state=ShadowEvidenceState.SUSPENDED,
        effective_at=observations[3].observed_at,
        available_at=observations[3].received_at,
        evidence_digest="9" * 64,
        reason="synthetic suspension",
    )
    extended = ShadowEvidenceManifest(
        dataset_digest=manifest.dataset_digest,
        return_matrix_digest=manifest.return_matrix_digest,
        events=tuple(
            sorted(
                (*manifest.events, suspension),
                key=lambda item: (
                    item.available_at,
                    item.candidate_id,
                    item.event_id,
                ),
            )
        ),
    )
    resumed = DeterministicForwardShadowRunner(
        evidence_manifest=extended,
        policy=policy,
        clock=FixedForwardShadowClock(now),
    ).process(observations[3:4], state=first.state)

    assert resumed.state.manifest_digest == extended.manifest_digest
    assert suspension.event_id in resumed.state.manifest_event_ids


def test_evidence_manifest_cannot_remove_prior_events() -> None:
    manifest, policy, observations, now = build_synthetic_forward_shadow()
    first_runner = DeterministicForwardShadowRunner(
        evidence_manifest=manifest,
        policy=policy,
        clock=FixedForwardShadowClock(now),
    )
    first = first_runner.process(observations[:2])
    reduced = ShadowEvidenceManifest(
        dataset_digest=manifest.dataset_digest,
        return_matrix_digest=manifest.return_matrix_digest,
        events=manifest.events[:-1],
    )
    resumed_runner = DeterministicForwardShadowRunner(
        evidence_manifest=reduced,
        policy=policy,
        clock=FixedForwardShadowClock(now),
    )

    with pytest.raises(ForwardShadowIntegrityError, match="removed or changed"):
        resumed_runner.process(observations[2:3], state=first.state)


def test_evidence_manifest_cannot_backfill_new_events() -> None:
    manifest, policy, observations, now = build_synthetic_forward_shadow()
    first_runner = DeterministicForwardShadowRunner(
        evidence_manifest=manifest,
        policy=policy,
        clock=FixedForwardShadowClock(now),
    )
    first = first_runner.process(observations[:3])
    backfilled = ShadowEvidenceEvent(
        candidate_id="trend",
        strategy_name="synthetic-trend",
        strategy_version="1",
        state=ShadowEvidenceState.SUSPENDED,
        effective_at=observations[1].observed_at,
        available_at=observations[1].received_at,
        evidence_digest="8" * 64,
        reason="invalid backfilled suspension",
    )
    extended = ShadowEvidenceManifest(
        dataset_digest=manifest.dataset_digest,
        return_matrix_digest=manifest.return_matrix_digest,
        events=tuple(
            sorted(
                (*manifest.events, backfilled),
                key=lambda item: (
                    item.available_at,
                    item.candidate_id,
                    item.event_id,
                ),
            )
        ),
    )
    resumed_runner = DeterministicForwardShadowRunner(
        evidence_manifest=extended,
        policy=policy,
        clock=FixedForwardShadowClock(now),
    )

    with pytest.raises(ForwardShadowIntegrityError, match="backfilled"):
        resumed_runner.process(observations[3:4], state=first.state)
