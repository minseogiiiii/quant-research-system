from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from world_quant_system.paper_execution.models import (
    KillSwitchMode,
    OrderEventType,
    PaperExecutionIntegrityError,
    PaperMarketSnapshot,
    PaperOrderIntent,
    PaperOrderSide,
    PaperOrderStatus,
)
from world_quant_system.paper_execution.store import SQLitePaperExecutionStore


def _intent(now: datetime) -> PaperOrderIntent:
    market = PaperMarketSnapshot(
        symbol="ABC",
        bid=Decimal("99"),
        ask=Decimal("100"),
        last=Decimal("99.5"),
        captured_at=now,
        source_digest="c" * 64,
    )
    return PaperOrderIntent(
        decision_id="decision-1",
        symbol="ABC",
        side=PaperOrderSide.BUY,
        quantity=5,
        limit_price=Decimal("100"),
        created_at=now,
        market_snapshot_digest=market.snapshot_digest,
        strategy_id="strategy-1",
        reason="store test",
    )


def test_store_persists_hash_chained_state(tmp_path: Path) -> None:
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    store = SQLitePaperExecutionStore(tmp_path / "paper.sqlite3")
    intent = _intent(now)
    created, was_created = store.create_intent(intent, created_at=now)
    assert was_created
    assert created.status == PaperOrderStatus.CREATED
    validated = store.transition(
        intent.client_order_id,
        target_status=PaperOrderStatus.VALIDATED,
        event_type=OrderEventType.PRETRADE_APPROVED,
        reason="approved",
        updated_at=now,
    )
    submitting = store.transition(
        intent.client_order_id,
        target_status=PaperOrderStatus.SUBMITTING,
        event_type=OrderEventType.SUBMISSION_STARTED,
        reason="submitting",
        updated_at=now,
    )
    submitted = store.transition(
        intent.client_order_id,
        target_status=PaperOrderStatus.SUBMITTED,
        event_type=OrderEventType.SUBMISSION_CONFIRMED,
        reason="submitted",
        updated_at=now,
        broker_order_id="paper-order-1",
    )
    assert validated.version == 1
    assert submitting.version == 2
    assert submitted.version == 3
    events = store.list_events(intent.client_order_id)
    assert len(events) == 4
    assert events[0].previous_hash == "0" * 64
    assert events[-1].previous_hash == events[-2].event_hash
    reloaded = SQLitePaperExecutionStore(tmp_path / "paper.sqlite3")
    assert reloaded.get_order(intent.client_order_id) == submitted


def test_store_rejects_invalid_terminal_transition(tmp_path: Path) -> None:
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    store = SQLitePaperExecutionStore(tmp_path / "paper.sqlite3")
    intent = _intent(now)
    store.create_intent(intent, created_at=now)
    store.transition(
        intent.client_order_id,
        target_status=PaperOrderStatus.REJECTED,
        event_type=OrderEventType.PRETRADE_REJECTED,
        reason="rejected",
        updated_at=now,
        rejection_reason="rejected",
    )
    with pytest.raises(PaperExecutionIntegrityError, match="invalid"):
        store.transition(
            intent.client_order_id,
            target_status=PaperOrderStatus.SUBMITTED,
            event_type=OrderEventType.SUBMISSION_CONFIRMED,
            reason="invalid",
            updated_at=now,
            broker_order_id="paper-order-1",
        )


def test_kill_switch_only_escalates_without_explicit_reset(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    store = SQLitePaperExecutionStore(tmp_path / "paper.sqlite3")
    soft = store.activate_kill_switch(
        KillSwitchMode.SOFT_HALT,
        reason="soft",
        activated_at=now,
    )
    still_soft = store.activate_kill_switch(
        KillSwitchMode.SOFT_HALT,
        reason="duplicate",
        activated_at=now,
    )
    hard = store.activate_kill_switch(
        KillSwitchMode.HARD_HALT,
        reason="hard",
        activated_at=now,
    )
    assert soft.mode == KillSwitchMode.SOFT_HALT
    assert still_soft.mode == KillSwitchMode.SOFT_HALT
    assert hard.mode == KillSwitchMode.HARD_HALT
    reset = store.reset_kill_switch(
        reason="human reviewed reconciliation",
        reset_at=now,
        expected_version=hard.version,
    )
    assert reset.mode == KillSwitchMode.NORMAL
    with pytest.raises(PaperExecutionIntegrityError, match="version"):
        store.reset_kill_switch(
            reason="stale reset",
            reset_at=now,
            expected_version=hard.version,
        )


def test_duplicate_intent_is_idempotent(tmp_path: Path) -> None:
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    store = SQLitePaperExecutionStore(tmp_path / "paper.sqlite3")
    intent = _intent(now)
    first, first_created = store.create_intent(intent, created_at=now)
    second, second_created = store.create_intent(intent, created_at=now)
    assert first_created
    assert not second_created
    assert first == second
    assert len(store.list_events(intent.client_order_id)) == 1
