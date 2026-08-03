from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from world_quant_system.paper_execution.gateway import (
    FixedPaperExecutionClock,
    PaperOrderCoordinator,
    PreTradeRiskGateway,
)
from world_quant_system.paper_execution.models import (
    BrokerCapabilityProfile,
    KillSwitchMode,
    PaperAccountSnapshot,
    PaperBrokerEnvironment,
    PaperCashBalance,
    PaperExecutionPolicy,
    PaperExecutionSafetyError,
    PaperMarketSnapshot,
    PaperOrderIntent,
    PaperOrderSide,
    PaperOrderStatus,
)
from world_quant_system.paper_execution.simulation import (
    DeterministicInMemoryPaperBroker,
)
from world_quant_system.paper_execution.store import SQLitePaperExecutionStore


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


def _policy() -> PaperExecutionPolicy:
    return PaperExecutionPolicy(
        expected_provider="synthetic-paper",
        expected_environment=PaperBrokerEnvironment.SANDBOX,
        expected_account_fingerprint="a" * 64,
        expected_endpoint_fingerprint="b" * 64,
        currency="USD",
        allowed_symbols=("ABC",),
        maximum_market_age_seconds=60,
        maximum_account_age_seconds=60,
        maximum_order_quantity=100,
        maximum_order_notional=Decimal("10000"),
        maximum_position_quantity=200,
        maximum_open_orders=5,
        minimum_cash_reserve_fraction=Decimal("0.1"),
        maximum_limit_deviation_bps=Decimal("100"),
        cash_reconciliation_tolerance=Decimal("0.01"),
    )


def _market(now: datetime) -> PaperMarketSnapshot:
    return PaperMarketSnapshot(
        symbol="ABC",
        bid=Decimal("99.9"),
        ask=Decimal("100"),
        last=Decimal("99.95"),
        captured_at=now,
        source_digest="c" * 64,
    )


def _intent(now: datetime, market: PaperMarketSnapshot) -> PaperOrderIntent:
    return PaperOrderIntent(
        decision_id="decision-1",
        symbol="ABC",
        side=PaperOrderSide.BUY,
        quantity=10,
        limit_price=Decimal("100"),
        created_at=now,
        market_snapshot_digest=market.snapshot_digest,
        strategy_id="strategy-1",
        reason="paper test",
    )


def _account(now: datetime, cash: Decimal) -> PaperAccountSnapshot:
    profile = _profile()
    return PaperAccountSnapshot(
        profile=profile,
        captured_at=now,
        cash=PaperCashBalance(
            currency="USD",
            settled_cash=cash,
            buying_power=cash,
        ),
        positions=(),
        orders=(),
        fills=(),
        source_digest="d" * 64,
    )


def test_pretrade_approves_safe_marketable_limit() -> None:
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    market = _market(now)
    decision = PreTradeRiskGateway().evaluate(
        intent=_intent(now, market),
        market=market,
        account=_account(now, Decimal("100000")),
        policy=_policy(),
        kill_switch_mode=KillSwitchMode.NORMAL,
        evaluated_at=now,
        existing_client_order_ids=frozenset(),
    )
    assert decision.approved
    assert decision.reasons == ()
    assert decision.projected_position_quantity == 10


def test_pretrade_rejects_stale_data_and_insufficient_cash() -> None:
    now = datetime(2026, 8, 3, 20, 2, tzinfo=UTC)
    old = now - timedelta(minutes=2)
    market = _market(old)
    decision = PreTradeRiskGateway().evaluate(
        intent=_intent(old, market),
        market=market,
        account=_account(old, Decimal("1000")),
        policy=_policy(),
        kill_switch_mode=KillSwitchMode.NORMAL,
        evaluated_at=now,
        existing_client_order_ids=frozenset(),
    )
    assert not decision.approved
    assert "market_snapshot_stale" in decision.reasons
    assert "account_snapshot_stale" in decision.reasons
    assert "insufficient_cash_after_required_reserve" in decision.reasons


def test_coordinator_is_idempotent(tmp_path: Path) -> None:
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    market = _market(now)
    intent = _intent(now, market)
    broker = DeterministicInMemoryPaperBroker(
        profile=_profile(),
        captured_at=now,
        cash=Decimal("100000"),
    )
    store = SQLitePaperExecutionStore(tmp_path / "paper.sqlite3")
    coordinator = PaperOrderCoordinator(
        broker=broker,
        store=store,
        policy=_policy(),
        clock=FixedPaperExecutionClock(now),
    )
    first = asyncio.run(coordinator.submit(intent, market=market))
    second = asyncio.run(coordinator.submit(intent, market=market))
    assert first.status == PaperOrderStatus.SUBMITTED
    assert second == first
    assert broker.submit_calls == 1


def test_timeout_after_acceptance_recovers_without_resubmission(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    market = _market(now)
    intent = _intent(now, market)
    broker = DeterministicInMemoryPaperBroker(
        profile=_profile(),
        captured_at=now,
        cash=Decimal("100000"),
    )
    broker.timeout_after_accept_once()
    store = SQLitePaperExecutionStore(tmp_path / "paper.sqlite3")
    coordinator = PaperOrderCoordinator(
        broker=broker,
        store=store,
        policy=_policy(),
        clock=FixedPaperExecutionClock(now),
    )
    with pytest.raises(PaperExecutionSafetyError, match="unknown"):
        asyncio.run(coordinator.submit(intent, market=market))
    assert store.get_kill_switch().mode == KillSwitchMode.SOFT_HALT
    recovered = asyncio.run(coordinator.recover_unknown(intent.client_order_id))
    assert recovered.status == PaperOrderStatus.SUBMITTED
    assert broker.submit_calls == 1


def test_pretrade_rejects_duplicate_and_active_kill_switch() -> None:
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    market = _market(now)
    intent = _intent(now, market)
    decision = PreTradeRiskGateway().evaluate(
        intent=intent,
        market=market,
        account=_account(now, Decimal("100000")),
        policy=_policy(),
        kill_switch_mode=KillSwitchMode.SOFT_HALT,
        evaluated_at=now,
        existing_client_order_ids=frozenset({intent.client_order_id}),
    )
    assert not decision.approved
    assert "duplicate_client_order_id" in decision.reasons
    assert "kill_switch_soft_halt" in decision.reasons


def test_cancel_only_mode_allows_cancel_but_blocks_submission(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    market = _market(now)
    intent = _intent(now, market)
    broker = DeterministicInMemoryPaperBroker(
        profile=_profile(),
        captured_at=now,
        cash=Decimal("100000"),
    )
    store = SQLitePaperExecutionStore(tmp_path / "paper.sqlite3")
    coordinator = PaperOrderCoordinator(
        broker=broker,
        store=store,
        policy=_policy(),
        clock=FixedPaperExecutionClock(now),
    )
    submitted = asyncio.run(coordinator.submit(intent, market=market))
    assert submitted.status == PaperOrderStatus.SUBMITTED
    store.activate_kill_switch(
        KillSwitchMode.CANCEL_ONLY,
        reason="reconciliation review",
        activated_at=now,
    )
    canceled = asyncio.run(coordinator.cancel(intent.client_order_id))
    assert canceled.status == PaperOrderStatus.CANCELED
    assert broker.cancel_calls == 1
