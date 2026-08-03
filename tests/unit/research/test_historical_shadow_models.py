from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from world_quant_system.research.historical_shadow_models import (
    HistoricalShadowConfigurationError,
    HistoricalShadowPolicy,
    ShadowEvidenceEvent,
    ShadowEvidenceManifest,
    ShadowEvidenceState,
)

START = datetime(2024, 1, 1, tzinfo=UTC)


def event(
    candidate_id: str,
    *,
    state: ShadowEvidenceState = ShadowEvidenceState.PROMOTED,
    available_at: datetime = START,
    character: str = "a",
) -> ShadowEvidenceEvent:
    return ShadowEvidenceEvent(
        candidate_id=candidate_id,
        strategy_name=f"strategy-{candidate_id}",
        strategy_version="1",
        state=state,
        effective_at=available_at,
        available_at=available_at,
        evidence_digest=character * 64,
        reason="unit-test evidence",
    )


def test_evidence_rejects_availability_before_effective_time() -> None:
    with pytest.raises(
        HistoricalShadowConfigurationError,
        match="available before",
    ):
        ShadowEvidenceEvent(
            candidate_id="alpha",
            strategy_name="alpha",
            strategy_version="1",
            state=ShadowEvidenceState.PROMOTED,
            effective_at=START + timedelta(days=1),
            available_at=START,
            evidence_digest="a" * 64,
            reason="invalid timing",
        )


def test_manifest_requires_strict_candidate_event_order() -> None:
    later = event("alpha", available_at=START + timedelta(days=2))
    earlier = event("alpha", available_at=START, character="b")
    with pytest.raises(HistoricalShadowConfigurationError, match="sorted"):
        ShadowEvidenceManifest(
            dataset_digest="d" * 64,
            return_matrix_digest="e" * 64,
            events=(later, earlier),
        )


def test_policy_rejects_same_period_execution() -> None:
    with pytest.raises(
        HistoricalShadowConfigurationError,
        match="Execution delay periods",
    ):
        HistoricalShadowPolicy(execution_delay_periods=0)


def test_policy_rejects_nonnegative_drawdown_halt() -> None:
    with pytest.raises(
        HistoricalShadowConfigurationError,
        match="halt threshold",
    ):
        HistoricalShadowPolicy(
            maximum_drawdown_before_halt=Decimal("0"),
        )
