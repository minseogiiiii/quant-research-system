from __future__ import annotations

from datetime import UTC, datetime, timedelta

from world_quant_system.paper_execution.models import (
    DiscrepancySeverity,
    InternalOrderState,
    InternalPaperLedgerSnapshot,
    KillSwitchMode,
    PaperAccountSnapshot,
    PaperCashBalance,
    PaperExecutionPolicy,
    PaperOrderStatus,
    PaperPosition,
    ReconciliationDecision,
    ReconciliationDiscrepancy,
    ReconciliationReport,
    is_terminal_status,
)
from world_quant_system.paper_execution.store import SQLitePaperExecutionStore


class DeterministicPaperReconciler:
    """Compare internal and broker paper state without silently mutating either."""

    def reconcile(
        self,
        *,
        internal: InternalPaperLedgerSnapshot,
        broker: PaperAccountSnapshot,
        policy: PaperExecutionPolicy,
        compared_at: datetime,
    ) -> ReconciliationReport:
        discrepancies: list[ReconciliationDiscrepancy] = []
        if internal.account_fingerprint != broker.profile.account_fingerprint:
            discrepancies.append(
                _critical(
                    "account_fingerprint_mismatch",
                    "Internal and broker snapshots refer to different accounts.",
                )
            )
        if broker.profile.account_fingerprint != policy.expected_account_fingerprint:
            discrepancies.append(
                _critical(
                    "unexpected_broker_account",
                    "Broker account differs from the certified policy account.",
                )
            )
        if broker.profile.environment != policy.expected_environment:
            discrepancies.append(
                _critical(
                    "unexpected_broker_environment",
                    "Broker environment differs from the paper policy.",
                )
            )
        if broker.profile.endpoint_fingerprint != policy.expected_endpoint_fingerprint:
            discrepancies.append(
                _critical(
                    "unexpected_broker_endpoint",
                    "Broker endpoint fingerprint differs from certification.",
                )
            )
        if internal.cash.currency != broker.cash.currency:
            discrepancies.append(
                _critical(
                    "cash_currency_mismatch",
                    "Internal and broker cash currencies differ.",
                )
            )
        cash_difference = abs(
            internal.cash.settled_cash - broker.cash.settled_cash
        )
        if cash_difference > policy.cash_reconciliation_tolerance:
            discrepancies.append(
                _critical(
                    "cash_balance_mismatch",
                    "Internal and broker settled cash differ beyond tolerance.",
                )
            )
        age = compared_at.astimezone(UTC) - broker.captured_at.astimezone(UTC)
        if age < timedelta(0):
            discrepancies.append(
                _critical(
                    "broker_snapshot_from_future",
                    "Broker account snapshot is dated in the future.",
                )
            )
        elif age > timedelta(seconds=policy.maximum_account_age_seconds):
            discrepancies.append(
                _critical(
                    "broker_snapshot_stale",
                    "Broker account snapshot is too old for reconciliation.",
                )
            )
        discrepancies.extend(_compare_positions(internal, broker))
        discrepancies.extend(_compare_orders(internal, broker))
        discrepancies.extend(_compare_fills(internal, broker))
        ordered = tuple(
            sorted(
                discrepancies,
                key=lambda item: (
                    item.severity.value,
                    item.code,
                    item.symbol or "",
                    item.client_order_id or "",
                ),
            )
        )
        critical = any(
            item.severity == DiscrepancySeverity.CRITICAL for item in ordered
        )
        if critical:
            decision = ReconciliationDecision.HALT
            kill_switch = KillSwitchMode.HARD_HALT
        elif ordered:
            decision = ReconciliationDecision.MANUAL_REVIEW_REQUIRED
            kill_switch = KillSwitchMode.SOFT_HALT
        else:
            decision = ReconciliationDecision.PASS
            kill_switch = KillSwitchMode.NORMAL
        return ReconciliationReport(
            internal_snapshot_digest=internal.snapshot_digest,
            broker_snapshot_digest=broker.snapshot_digest,
            policy_digest=policy.policy_digest,
            compared_at=compared_at,
            discrepancies=ordered,
            decision=decision,
            recommended_kill_switch=kill_switch,
        )

    def reconcile_and_record(
        self,
        *,
        internal: InternalPaperLedgerSnapshot,
        broker: PaperAccountSnapshot,
        policy: PaperExecutionPolicy,
        compared_at: datetime,
        store: SQLitePaperExecutionStore,
    ) -> ReconciliationReport:
        report = self.reconcile(
            internal=internal,
            broker=broker,
            policy=policy,
            compared_at=compared_at,
        )
        store.record_reconciliation(report)
        if report.recommended_kill_switch != KillSwitchMode.NORMAL:
            store.activate_kill_switch(
                report.recommended_kill_switch,
                reason=(
                    "paper reconciliation failed: "
                    + ",".join(item.code for item in report.discrepancies)
                ),
                activated_at=compared_at,
            )
        return report


def build_internal_ledger_snapshot(
    *,
    store: SQLitePaperExecutionStore,
    account_fingerprint: str,
    captured_at: datetime,
    cash: PaperCashBalance,
    positions: tuple[PaperPosition, ...],
    fill_ids: tuple[str, ...],
) -> InternalPaperLedgerSnapshot:
    orders = tuple(
        InternalOrderState(
            client_order_id=item.intent.client_order_id,
            broker_order_id=item.broker_order_id,
            status=item.status,
            filled_quantity=item.filled_quantity,
        )
        for item in store.list_orders()
    )
    return InternalPaperLedgerSnapshot(
        account_fingerprint=account_fingerprint,
        captured_at=captured_at,
        cash=cash,
        positions=positions,
        orders=orders,
        fill_ids=fill_ids,
    )


def _compare_positions(
    internal: InternalPaperLedgerSnapshot,
    broker: PaperAccountSnapshot,
) -> list[ReconciliationDiscrepancy]:
    discrepancies: list[ReconciliationDiscrepancy] = []
    symbols = sorted(
        set(internal.positions_by_symbol) | set(broker.positions_by_symbol)
    )
    for symbol in symbols:
        internal_position = internal.positions_by_symbol.get(symbol)
        broker_position = broker.positions_by_symbol.get(symbol)
        if internal_position is None:
            discrepancies.append(
                _critical(
                    "unknown_broker_position",
                    "Broker reports a position absent from the internal ledger.",
                    symbol=symbol,
                )
            )
            continue
        if broker_position is None:
            discrepancies.append(
                _critical(
                    "missing_broker_position",
                    "Internal ledger position is absent from the broker snapshot.",
                    symbol=symbol,
                )
            )
            continue
        if internal_position.quantity != broker_position.quantity:
            discrepancies.append(
                _critical(
                    "position_quantity_mismatch",
                    "Internal and broker position quantities differ.",
                    symbol=symbol,
                )
            )
        if internal_position.average_price != broker_position.average_price:
            discrepancies.append(
                _warning(
                    "position_average_price_mismatch",
                    "Internal and broker average position prices differ.",
                    symbol=symbol,
                )
            )
    return discrepancies


def _compare_orders(
    internal: InternalPaperLedgerSnapshot,
    broker: PaperAccountSnapshot,
) -> list[ReconciliationDiscrepancy]:
    discrepancies: list[ReconciliationDiscrepancy] = []
    internal_orders = internal.orders_by_client_id
    broker_orders = broker.orders_by_client_id
    for client_order_id in sorted(set(internal_orders) | set(broker_orders)):
        internal_order = internal_orders.get(client_order_id)
        broker_order = broker_orders.get(client_order_id)
        if internal_order is None:
            discrepancies.append(
                _critical(
                    "unknown_broker_order",
                    "Broker reports an order absent from the internal store.",
                    client_order_id=client_order_id,
                )
            )
            continue
        if broker_order is None:
            if internal_order.broker_order_id is not None or internal_order.status in {
                PaperOrderStatus.SUBMITTED,
                PaperOrderStatus.PARTIALLY_FILLED,
                PaperOrderStatus.FILLED,
                PaperOrderStatus.CANCEL_PENDING,
                PaperOrderStatus.CANCELED,
                PaperOrderStatus.UNKNOWN,
            }:
                discrepancies.append(
                    _critical(
                        "missing_broker_order",
                        "Internal submitted order is absent from broker snapshot.",
                        client_order_id=client_order_id,
                    )
                )
            continue
        if internal_order.broker_order_id != broker_order.broker_order_id:
            discrepancies.append(
                _critical(
                    "broker_order_id_mismatch",
                    "Internal and broker order IDs differ.",
                    client_order_id=client_order_id,
                )
            )
        if internal_order.filled_quantity != broker_order.filled_quantity:
            discrepancies.append(
                _critical(
                    "filled_quantity_mismatch",
                    "Internal and broker filled quantities differ.",
                    client_order_id=client_order_id,
                )
            )
        if internal_order.status != broker_order.status:
            severity = (
                DiscrepancySeverity.CRITICAL
                if is_terminal_status(internal_order.status)
                else DiscrepancySeverity.WARNING
            )
            discrepancies.append(
                ReconciliationDiscrepancy(
                    code="order_status_mismatch",
                    severity=severity,
                    message="Internal and broker order states differ.",
                    client_order_id=client_order_id,
                )
            )
    return discrepancies


def _compare_fills(
    internal: InternalPaperLedgerSnapshot,
    broker: PaperAccountSnapshot,
) -> list[ReconciliationDiscrepancy]:
    known_fill_ids = set(internal.fill_ids)
    return [
        _critical(
            "unknown_broker_fill",
            "Broker reports a fill absent from the internal ledger.",
            client_order_id=fill.client_order_id,
        )
        for fill in broker.fills
        if fill.fill_id not in known_fill_ids
    ]


def _critical(
    code: str,
    message: str,
    *,
    symbol: str | None = None,
    client_order_id: str | None = None,
) -> ReconciliationDiscrepancy:
    return ReconciliationDiscrepancy(
        code=code,
        severity=DiscrepancySeverity.CRITICAL,
        message=message,
        symbol=symbol,
        client_order_id=client_order_id,
    )


def _warning(
    code: str,
    message: str,
    *,
    symbol: str | None = None,
    client_order_id: str | None = None,
) -> ReconciliationDiscrepancy:
    return ReconciliationDiscrepancy(
        code=code,
        severity=DiscrepancySeverity.WARNING,
        message=message,
        symbol=symbol,
        client_order_id=client_order_id,
    )
