from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from world_quant_system.paper_execution.models import (
    BrokerCapabilityProfile,
    PaperBrokerEnvironment,
    PaperExecutionConfigurationError,
    PaperExecutionIntegrityError,
    PaperExecutionSafetyError,
    PaperMarketSnapshot,
    PaperOrderIntent,
    PaperOrderSide,
    PaperOrderStatus,
    parse_capability_profile,
    validate_order_transition,
)


def _profile() -> BrokerCapabilityProfile:
    return BrokerCapabilityProfile(
        provider="synthetic-paper",
        environment=PaperBrokerEnvironment.SANDBOX,
        account_fingerprint="a" * 64,
        endpoint_fingerprint="b" * 64,
        currency="USD",
        read_only_access=True,
        paper_order_submission=True,
        cancellation=True,
        client_order_id_idempotency=True,
    )


def test_paper_environment_has_no_live_value() -> None:
    with pytest.raises(
        PaperExecutionConfigurationError,
        match="unsupported value",
    ):
        parse_capability_profile(
            {
                **_profile().to_document(),
                "environment": "live",
            }
        )


def test_paper_profile_rejects_dangerous_capabilities() -> None:
    with pytest.raises(PaperExecutionSafetyError, match="forbids"):
        BrokerCapabilityProfile(
            provider="synthetic-paper",
            environment=PaperBrokerEnvironment.PAPER,
            account_fingerprint="a" * 64,
            endpoint_fingerprint="b" * 64,
            currency="USD",
            read_only_access=True,
            paper_order_submission=True,
            cancellation=True,
            client_order_id_idempotency=True,
            margin=True,
        )


def test_order_intent_identity_is_deterministic() -> None:
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    market = PaperMarketSnapshot(
        symbol="ABC",
        bid=Decimal("99"),
        ask=Decimal("100"),
        last=Decimal("99.5"),
        captured_at=now,
        source_digest="c" * 64,
    )
    first = PaperOrderIntent(
        decision_id="decision-1",
        symbol="ABC",
        side=PaperOrderSide.BUY,
        quantity=5,
        limit_price=Decimal("100"),
        created_at=now,
        market_snapshot_digest=market.snapshot_digest,
        strategy_id="strategy-1",
        reason="paper test",
    )
    second = PaperOrderIntent(
        decision_id="decision-1",
        symbol="ABC",
        side=PaperOrderSide.BUY,
        quantity=5,
        limit_price=Decimal("100.0"),
        created_at=now,
        market_snapshot_digest=market.snapshot_digest,
        strategy_id="strategy-1",
        reason="paper test",
    )
    assert first.intent_id == second.intent_id
    assert first.client_order_id == second.client_order_id
    assert first.client_order_id.startswith("wqs-")
    assert len(first.client_order_id) == 32


def test_terminal_order_transition_is_rejected() -> None:
    with pytest.raises(PaperExecutionIntegrityError, match="invalid"):
        validate_order_transition(
            PaperOrderStatus.FILLED,
            PaperOrderStatus.SUBMITTED,
        )
