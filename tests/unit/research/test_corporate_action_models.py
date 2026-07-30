from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from world_quant_system.research import (
    CorporateActionConfigurationError,
    CorporateActionEventPhase,
    CorporateActionPolicy,
    CorporateActionRecord,
    CorporateActionType,
)

START = datetime(2020, 1, 1, tzinfo=UTC)


def test_split_record_has_deterministic_identity_and_event() -> None:
    first = CorporateActionRecord(
        exchange="xkrx",
        symbol="005930",
        action_type=CorporateActionType.SPLIT,
        effective_at=START + timedelta(days=2),
        available_at=START,
        source="test",
        source_digest="a" * 64,
        ratio_numerator=2,
        ratio_denominator=1,
    )
    second = CorporateActionRecord(
        exchange="XKRX",
        symbol="005930",
        action_type=CorporateActionType.SPLIT,
        effective_at=START + timedelta(days=2),
        available_at=START,
        source="test",
        source_digest="a" * 64,
        ratio_numerator=2,
        ratio_denominator=1,
    )

    assert first == second
    assert first.action_id == second.action_id
    assert first.events[0].phase is CorporateActionEventPhase.APPLY
    assert first.events[0].event_at == first.effective_at


def test_cash_dividend_creates_entitlement_and_payment_events() -> None:
    declared_at = START
    ex_at = START + timedelta(days=2)
    record_at = START + timedelta(days=3)
    payment_at = START + timedelta(days=7)
    action = CorporateActionRecord(
        exchange="XKRX",
        symbol="005930",
        action_type=CorporateActionType.CASH_DIVIDEND,
        effective_at=ex_at,
        available_at=declared_at,
        source="test",
        source_digest="b" * 64,
        cash_amount_per_share=Decimal("100"),
        declared_at=declared_at,
        ex_at=ex_at,
        record_at=record_at,
        payment_at=payment_at,
    )

    assert tuple(event.phase for event in action.events) == (
        CorporateActionEventPhase.DIVIDEND_ENTITLEMENT,
        CorporateActionEventPhase.DIVIDEND_PAYMENT,
    )
    assert tuple(event.event_at for event in action.events) == (ex_at, payment_at)


def test_delisting_requires_exactly_one_settlement_method() -> None:
    with pytest.raises(CorporateActionConfigurationError):
        CorporateActionRecord(
            exchange="XKRX",
            symbol="005930",
            action_type=CorporateActionType.DELISTING,
            effective_at=START + timedelta(days=2),
            available_at=START,
            source="test",
            source_digest="c" * 64,
        )

    with pytest.raises(CorporateActionConfigurationError):
        CorporateActionRecord(
            exchange="XKRX",
            symbol="005930",
            action_type=CorporateActionType.DELISTING,
            effective_at=START + timedelta(days=2),
            available_at=START,
            source="test",
            source_digest="c" * 64,
            delisting_cash_price=Decimal("0"),
            delisting_recovery_rate=Decimal("0.25"),
        )


def test_v1_requires_explicit_delisting_policy_and_valid_recovery_rate() -> None:
    with pytest.raises(CorporateActionConfigurationError):
        CorporateActionPolicy(require_explicit_delisting_value=False)

    with pytest.raises(CorporateActionConfigurationError):
        CorporateActionRecord(
            exchange="XNAS",
            symbol="ABC",
            action_type=CorporateActionType.DELISTING,
            effective_at=START + timedelta(days=2),
            available_at=START,
            source="test",
            source_digest="d" * 64,
            delisting_recovery_rate=Decimal("1.01"),
        )


def test_dividend_cannot_be_available_before_its_declaration() -> None:
    declared_at = START + timedelta(days=1)
    ex_at = START + timedelta(days=2)
    with pytest.raises(CorporateActionConfigurationError):
        CorporateActionRecord(
            exchange="XNAS",
            symbol="ABC",
            action_type=CorporateActionType.CASH_DIVIDEND,
            effective_at=ex_at,
            available_at=START,
            source="test",
            source_digest="e" * 64,
            cash_amount_per_share=Decimal("1"),
            declared_at=declared_at,
            ex_at=ex_at,
            record_at=START + timedelta(days=3),
            payment_at=START + timedelta(days=4),
        )
