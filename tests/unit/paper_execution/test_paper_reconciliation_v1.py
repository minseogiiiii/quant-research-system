from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from world_quant_system.paper_execution.models import (
    BrokerCapabilityProfile,
    BrokerOrderSnapshot,
    InternalOrderState,
    InternalPaperLedgerSnapshot,
    KillSwitchMode,
    PaperAccountSnapshot,
    PaperBrokerEnvironment,
    PaperCashBalance,
    PaperExecutionPolicy,
    PaperOrderSide,
    PaperOrderStatus,
    ReconciliationDecision,
)
from world_quant_system.paper_execution.reconciliation import (
    DeterministicPaperReconciler,
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


def _cash() -> PaperCashBalance:
    return PaperCashBalance(
        currency="USD",
        settled_cash=Decimal("100000"),
        buying_power=Decimal("100000"),
    )


def test_matching_snapshots_reconcile() -> None:
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    internal = InternalPaperLedgerSnapshot(
        account_fingerprint="a" * 64,
        captured_at=now,
        cash=_cash(),
        positions=(),
        orders=(),
        fill_ids=(),
    )
    broker = PaperAccountSnapshot(
        profile=_profile(),
        captured_at=now,
        cash=_cash(),
        positions=(),
        orders=(),
        fills=(),
        source_digest="c" * 64,
    )
    report = DeterministicPaperReconciler().reconcile(
        internal=internal,
        broker=broker,
        policy=_policy(),
        compared_at=now,
    )
    assert report.decision == ReconciliationDecision.PASS
    assert report.recommended_kill_switch == KillSwitchMode.NORMAL
    assert report.discrepancies == ()


def test_unknown_broker_order_triggers_hard_halt(tmp_path: Path) -> None:
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    internal = InternalPaperLedgerSnapshot(
        account_fingerprint="a" * 64,
        captured_at=now,
        cash=_cash(),
        positions=(),
        orders=(),
        fill_ids=(),
    )
    broker_order = BrokerOrderSnapshot(
        client_order_id="wqs-" + "1" * 28,
        broker_order_id="paper-unknown",
        symbol="ABC",
        side=PaperOrderSide.BUY,
        quantity=1,
        limit_price=Decimal("100"),
        filled_quantity=0,
        average_fill_price=None,
        status=PaperOrderStatus.SUBMITTED,
        submitted_at=now,
        updated_at=now,
    )
    broker = PaperAccountSnapshot(
        profile=_profile(),
        captured_at=now,
        cash=_cash(),
        positions=(),
        orders=(broker_order,),
        fills=(),
        source_digest="c" * 64,
    )
    store = SQLitePaperExecutionStore(tmp_path / "paper.sqlite3")
    report = DeterministicPaperReconciler().reconcile_and_record(
        internal=internal,
        broker=broker,
        policy=_policy(),
        compared_at=now,
        store=store,
    )
    assert report.decision == ReconciliationDecision.HALT
    assert report.recommended_kill_switch == KillSwitchMode.HARD_HALT
    assert store.get_kill_switch().mode == KillSwitchMode.HARD_HALT
    assert any(
        item.code == "unknown_broker_order"
        for item in report.discrepancies
    )


def test_nonterminal_status_difference_requires_manual_review() -> None:
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    client_order_id = "wqs-" + "2" * 28
    internal = InternalPaperLedgerSnapshot(
        account_fingerprint="a" * 64,
        captured_at=now,
        cash=_cash(),
        positions=(),
        orders=(
            InternalOrderState(
                client_order_id=client_order_id,
                broker_order_id="paper-1",
                status=PaperOrderStatus.SUBMITTED,
                filled_quantity=0,
            ),
        ),
        fill_ids=(),
    )
    broker = PaperAccountSnapshot(
        profile=_profile(),
        captured_at=now,
        cash=_cash(),
        positions=(),
        orders=(
            BrokerOrderSnapshot(
                client_order_id=client_order_id,
                broker_order_id="paper-1",
                symbol="ABC",
                side=PaperOrderSide.BUY,
                quantity=10,
                limit_price=Decimal("100"),
                filled_quantity=0,
                average_fill_price=None,
                status=PaperOrderStatus.CANCEL_PENDING,
                submitted_at=now,
                updated_at=now,
            ),
        ),
        fills=(),
        source_digest="c" * 64,
    )
    report = DeterministicPaperReconciler().reconcile(
        internal=internal,
        broker=broker,
        policy=_policy(),
        compared_at=now,
    )
    assert report.decision == ReconciliationDecision.MANUAL_REVIEW_REQUIRED
    assert report.recommended_kill_switch == KillSwitchMode.SOFT_HALT


def test_cash_mismatch_is_critical() -> None:
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    internal = InternalPaperLedgerSnapshot(
        account_fingerprint="a" * 64,
        captured_at=now,
        cash=_cash(),
        positions=(),
        orders=(),
        fill_ids=(),
    )
    broker = PaperAccountSnapshot(
        profile=_profile(),
        captured_at=now,
        cash=PaperCashBalance(
            currency="USD",
            settled_cash=Decimal("99999"),
            buying_power=Decimal("99999"),
        ),
        positions=(),
        orders=(),
        fills=(),
        source_digest="c" * 64,
    )
    report = DeterministicPaperReconciler().reconcile(
        internal=internal,
        broker=broker,
        policy=_policy(),
        compared_at=now,
    )
    assert report.decision == ReconciliationDecision.HALT
    assert any(
        item.code == "cash_balance_mismatch"
        for item in report.discrepancies
    )
