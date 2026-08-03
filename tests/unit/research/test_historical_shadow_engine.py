from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from world_quant_system.research.historical_shadow import (
    DeterministicHistoricalShadowRunner,
    PointInTimeEvidenceResolver,
)
from world_quant_system.research.historical_shadow_models import (
    HistoricalShadowIntegrityError,
    HistoricalShadowPolicy,
    ShadowEvidenceEvent,
    ShadowEvidenceManifest,
    ShadowEvidenceState,
    ShadowJournalEventType,
    ShadowScenarioName,
)
from world_quant_system.research.historical_shadow_simulation import (
    build_synthetic_run,
)
from world_quant_system.research.portfolio_promotion_models import (
    AllocationMethod,
    CandidateReturnMatrix,
)


def test_runner_rejects_return_matrix_digest_mismatch() -> None:
    manifest, matrix, policy = build_synthetic_run()
    mismatched = ShadowEvidenceManifest(
        dataset_digest=manifest.dataset_digest,
        return_matrix_digest="f" * 64,
        events=manifest.events,
    )

    with pytest.raises(HistoricalShadowIntegrityError, match="digest"):
        DeterministicHistoricalShadowRunner(
            evidence_manifest=mismatched,
            return_matrix=matrix,
            policy=policy,
        )


def test_point_in_time_resolver_hides_future_evidence() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    promoted = ShadowEvidenceEvent(
        candidate_id="alpha",
        strategy_name="alpha",
        strategy_version="1",
        state=ShadowEvidenceState.PROMOTED,
        effective_at=start + timedelta(days=2),
        available_at=start + timedelta(days=3),
        evidence_digest="a" * 64,
        reason="promotion completed",
    )
    manifest = ShadowEvidenceManifest(
        dataset_digest="d" * 64,
        return_matrix_digest="e" * 64,
        events=(promoted,),
    )
    resolver = PointInTimeEvidenceResolver(manifest)

    assert resolver.visible_event("alpha", start + timedelta(days=2)) is None
    assert resolver.visible_event("alpha", start + timedelta(days=3)) == promoted


def test_suspension_removes_candidate_after_availability() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    promoted = ShadowEvidenceEvent(
        candidate_id="alpha",
        strategy_name="alpha",
        strategy_version="1",
        state=ShadowEvidenceState.PROMOTED,
        effective_at=start,
        available_at=start,
        evidence_digest="a" * 64,
        reason="promoted",
    )
    suspended = ShadowEvidenceEvent(
        candidate_id="alpha",
        strategy_name="alpha",
        strategy_version="1",
        state=ShadowEvidenceState.SUSPENDED,
        effective_at=start + timedelta(days=5),
        available_at=start + timedelta(days=6),
        evidence_digest="b" * 64,
        reason="stability breach",
    )
    manifest = ShadowEvidenceManifest(
        dataset_digest="d" * 64,
        return_matrix_digest="e" * 64,
        events=(promoted, suspended),
    )
    resolver = PointInTimeEvidenceResolver(manifest)

    assert resolver.eligible_candidates(
        start + timedelta(days=5),
        maximum_age_days=30,
    ) == ("alpha",)
    assert resolver.eligible_candidates(
        start + timedelta(days=6),
        maximum_age_days=30,
    ) == ()


def test_allocation_executes_only_on_next_period() -> None:
    manifest, matrix, policy = build_synthetic_run()
    report = DeterministicHistoricalShadowRunner(
        evidence_manifest=manifest,
        return_matrix=matrix,
        policy=policy,
    ).run(created_at=datetime(2026, 1, 1, tzinfo=UTC))

    assert report.ledger[5].weights == ()
    assert report.ledger[6].weights
    scheduled = next(
        item
        for item in report.journal
        if item.event_type is ShadowJournalEventType.REBALANCE_SCHEDULED
    )
    executed = next(
        item
        for item in report.journal
        if item.event_type is ShadowJournalEventType.REBALANCE_EXECUTED
    )
    assert executed.timestamp > scheduled.timestamp


def test_double_cost_never_improves_final_equity() -> None:
    manifest, matrix, policy = build_synthetic_run()
    report = DeterministicHistoricalShadowRunner(
        evidence_manifest=manifest,
        return_matrix=matrix,
        policy=policy,
    ).run()
    scenarios = {item.scenario: item for item in report.scenarios}

    assert (
        scenarios[ShadowScenarioName.DOUBLE_COST].metrics.final_equity
        <= scenarios[ShadowScenarioName.BASE].metrics.final_equity
    )


def test_future_return_change_cannot_change_prior_allocations() -> None:
    manifest, matrix, policy = build_synthetic_run()
    changed_series = list(matrix.returns_by_candidate)
    final_series = list(changed_series[0])
    final_series[-1] = Decimal("0.50")
    changed_series[0] = tuple(final_series)
    changed_matrix = CandidateReturnMatrix(
        timestamps=matrix.timestamps,
        candidate_ids=matrix.candidate_ids,
        returns_by_candidate=tuple(changed_series),
    )
    first = DeterministicHistoricalShadowRunner(
        evidence_manifest=manifest,
        return_matrix=matrix,
        policy=policy,
    ).run()
    changed_manifest = ShadowEvidenceManifest(
        dataset_digest=manifest.dataset_digest,
        return_matrix_digest=changed_matrix.matrix_digest,
        events=manifest.events,
    )
    second = DeterministicHistoricalShadowRunner(
        evidence_manifest=changed_manifest,
        return_matrix=changed_matrix,
        policy=policy,
    ).run()

    assert tuple(item.weights for item in first.ledger[:-1]) == tuple(
        item.weights for item in second.ledger[:-1]
    )


def test_drawdown_halt_moves_portfolio_toward_cash() -> None:
    manifest, matrix, _ = build_synthetic_run()
    crash_series = tuple(
        tuple(
            Decimal("-0.20") if index == 20 else value
            for index, value in enumerate(series)
        )
        for series in matrix.returns_by_candidate
    )
    crash_matrix = CandidateReturnMatrix(
        timestamps=matrix.timestamps,
        candidate_ids=matrix.candidate_ids,
        returns_by_candidate=crash_series,
    )
    policy = HistoricalShadowPolicy(
        allocation_method=AllocationMethod.EQUAL_WEIGHT,
        rebalance_frequency=5,
        execution_delay_periods=1,
        minimum_active_candidates=2,
        maximum_candidate_weight=Decimal("0.40"),
        maximum_evidence_age_days=365,
        maximum_drawdown_before_halt=Decimal("-0.10"),
        minimum_observations=60,
        minimum_base_cumulative_return=Decimal("-1"),
        minimum_stress_cumulative_return=Decimal("-1"),
        maximum_stress_drawdown=Decimal("-0.99"),
    )
    crash_manifest = ShadowEvidenceManifest(
        dataset_digest=manifest.dataset_digest,
        return_matrix_digest=crash_matrix.matrix_digest,
        events=manifest.events,
    )
    report = DeterministicHistoricalShadowRunner(
        evidence_manifest=crash_manifest,
        return_matrix=crash_matrix,
        policy=policy,
    ).run()

    halt_entry = next(
        item
        for item in report.journal
        if item.event_type is ShadowJournalEventType.RISK_HALT_TRIGGERED
    )
    halt_index = matrix.timestamps.index(halt_entry.timestamp)
    assert report.ledger[halt_index].risk_halted is True
    assert report.ledger[halt_index + 1].cash_weight == Decimal("1")
