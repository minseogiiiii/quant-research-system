from datetime import UTC, datetime
from decimal import Decimal

import pytest

from world_quant_system.research.forward_shadow_models import (
    ForwardShadowConfigurationError,
    ForwardShadowObservation,
    ForwardShadowPolicy,
    ForwardShadowReturn,
)


def test_observation_identity_is_deterministic() -> None:
    first = _observation()
    second = _observation()

    assert first.observation_id == second.observation_id
    assert first.to_document() == second.to_document()


def test_observation_requires_sorted_unique_returns() -> None:
    with pytest.raises(ForwardShadowConfigurationError, match="sorted"):
        ForwardShadowObservation(
            sequence=0,
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
            received_at=datetime(2026, 1, 1, tzinfo=UTC),
            returns=(
                ForwardShadowReturn("trend", Decimal("0.01")),
                ForwardShadowReturn("carry", Decimal("0.01")),
            ),
            source_digest="a" * 64,
        )


def test_policy_rejects_nonnegative_drawdown_halt() -> None:
    with pytest.raises(ForwardShadowConfigurationError, match="negative"):
        ForwardShadowPolicy(
            maximum_drawdown_before_halt=Decimal("0"),
        )


def _observation() -> ForwardShadowObservation:
    return ForwardShadowObservation(
        sequence=0,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        received_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        returns=(
            ForwardShadowReturn("carry", Decimal("0.01")),
            ForwardShadowReturn("trend", Decimal("-0.01")),
        ),
        source_digest="a" * 64,
    )
