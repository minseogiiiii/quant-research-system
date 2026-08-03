from __future__ import annotations

import asyncio
import hashlib
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from world_quant_system.paper_execution.gateway import (
    FixedPaperExecutionClock,
    PaperOrderCoordinator,
)
from world_quant_system.paper_execution.models import (
    BrokerCapabilityProfile,
    BrokerOrderSnapshot,
    KillSwitchMode,
    PaperAccountSnapshot,
    PaperBrokerEnvironment,
    PaperCashBalance,
    PaperExecutionPolicy,
    PaperExecutionSafetyError,
    PaperFillSnapshot,
    PaperMarketSnapshot,
    PaperOrderIntent,
    PaperOrderSide,
    PaperOrderStatus,
    PaperPosition,
    sha256_document,
)
from world_quant_system.paper_execution.reconciliation import (
    DeterministicPaperReconciler,
    build_internal_ledger_snapshot,
)
from world_quant_system.paper_execution.store import SQLitePaperExecutionStore


@dataclass(frozen=True, slots=True)
class PaperExecutionSimulationResult:
    first_status: PaperOrderStatus
    duplicate_status: PaperOrderStatus
    broker_submit_calls: int
    reconciliation_passed: bool
    timeout_recovered: bool
    kill_switch_after_timeout: KillSwitchMode
    live_trading_enabled: bool
    deterministic_digest: str


class DeterministicInMemoryPaperBroker:
    """Networkless paper broker used only for deterministic safety tests."""

    def __init__(
        self,
        *,
        profile: BrokerCapabilityProfile,
        captured_at: datetime,
        cash: Decimal,
        positions: tuple[PaperPosition, ...] = (),
        auto_fill: bool = False,
    ) -> None:
        self._profile = profile
        self._captured_at = captured_at
        self._cash = cash
        self._positions = {item.symbol: item for item in positions}
        self._orders: dict[str, BrokerOrderSnapshot] = {}
        self._fills: dict[str, PaperFillSnapshot] = {}
        self._auto_fill = auto_fill
        self._timeout_after_accept_once = False
        self.submit_calls = 0
        self.cancel_calls = 0

    def timeout_after_accept_once(self) -> None:
        self._timeout_after_accept_once = True

    def set_time(self, value: datetime) -> None:
        self._captured_at = value

    async def get_capability_profile(self) -> BrokerCapabilityProfile:
        return self._profile

    async def get_account_snapshot(self) -> PaperAccountSnapshot:
        source_digest = sha256_document(
            {
                "captured_at": self._captured_at.isoformat(),
                "cash": format(self._cash, "f"),
                "orders": [
                    item.to_document()
                    for item in sorted(
                        self._orders.values(),
                        key=lambda order: order.client_order_id,
                    )
                ],
            }
        )
        return PaperAccountSnapshot(
            profile=self._profile,
            captured_at=self._captured_at,
            cash=PaperCashBalance(
                currency=self._profile.currency,
                settled_cash=self._cash,
                buying_power=self._cash,
            ),
            positions=tuple(
                sorted(self._positions.values(), key=lambda item: item.symbol)
            ),
            orders=tuple(
                sorted(
                    self._orders.values(),
                    key=lambda item: item.client_order_id,
                )
            ),
            fills=tuple(
                sorted(self._fills.values(), key=lambda item: item.fill_id)
            ),
            source_digest=source_digest,
        )

    async def find_order_by_client_order_id(
        self,
        client_order_id: str,
    ) -> BrokerOrderSnapshot | None:
        return self._orders.get(client_order_id)

    async def submit_limit_order(
        self,
        intent: PaperOrderIntent,
    ) -> BrokerOrderSnapshot:
        existing = self._orders.get(intent.client_order_id)
        if existing is not None:
            return existing
        self.submit_calls += 1
        broker_order_id = f"paper-{intent.intent_id[:24]}"
        status = (
            PaperOrderStatus.FILLED
            if self._auto_fill
            else PaperOrderStatus.SUBMITTED
        )
        filled_quantity = intent.quantity if self._auto_fill else 0
        average_fill_price = intent.limit_price if self._auto_fill else None
        order = BrokerOrderSnapshot(
            client_order_id=intent.client_order_id,
            broker_order_id=broker_order_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            limit_price=intent.limit_price,
            filled_quantity=filled_quantity,
            average_fill_price=average_fill_price,
            status=status,
            submitted_at=self._captured_at,
            updated_at=self._captured_at,
        )
        self._orders[intent.client_order_id] = order
        if self._auto_fill:
            self._apply_fill(intent, broker_order_id)
        if self._timeout_after_accept_once:
            self._timeout_after_accept_once = False
            raise TimeoutError("synthetic timeout after broker acceptance")
        return order

    async def cancel_order(
        self,
        broker_order_id: str,
    ) -> BrokerOrderSnapshot:
        self.cancel_calls += 1
        matching = next(
            (
                item
                for item in self._orders.values()
                if item.broker_order_id == broker_order_id
            ),
            None,
        )
        if matching is None:
            raise KeyError(broker_order_id)
        canceled = BrokerOrderSnapshot(
            client_order_id=matching.client_order_id,
            broker_order_id=matching.broker_order_id,
            symbol=matching.symbol,
            side=matching.side,
            quantity=matching.quantity,
            limit_price=matching.limit_price,
            filled_quantity=matching.filled_quantity,
            average_fill_price=matching.average_fill_price,
            status=PaperOrderStatus.CANCELED,
            submitted_at=matching.submitted_at,
            updated_at=self._captured_at,
        )
        self._orders[matching.client_order_id] = canceled
        return canceled

    def _apply_fill(
        self,
        intent: PaperOrderIntent,
        broker_order_id: str,
    ) -> None:
        position = self._positions.get(intent.symbol)
        current_quantity = 0 if position is None else position.quantity
        if intent.side == PaperOrderSide.BUY:
            next_quantity = current_quantity + intent.quantity
            self._cash -= intent.notional
        else:
            next_quantity = current_quantity - intent.quantity
            self._cash += intent.notional
        if next_quantity == 0:
            self._positions.pop(intent.symbol, None)
        else:
            self._positions[intent.symbol] = PaperPosition(
                symbol=intent.symbol,
                quantity=next_quantity,
                average_price=intent.limit_price,
            )
        fill_id = hashlib.sha256(
            f"{broker_order_id}:1".encode()
        ).hexdigest()[:24]
        self._fills[fill_id] = PaperFillSnapshot(
            fill_id=fill_id,
            broker_order_id=broker_order_id,
            client_order_id=intent.client_order_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            price=intent.limit_price,
            commission=Decimal("0"),
            filled_at=self._captured_at,
        )


def run_paper_execution_simulation() -> PaperExecutionSimulationResult:
    return asyncio.run(_run_simulation())


async def _run_simulation() -> PaperExecutionSimulationResult:
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    account_fingerprint = "a" * 64
    endpoint_fingerprint = "b" * 64
    profile = BrokerCapabilityProfile(
        provider="synthetic-paper",
        environment=PaperBrokerEnvironment.SANDBOX,
        account_fingerprint=account_fingerprint,
        endpoint_fingerprint=endpoint_fingerprint,
        currency="USD",
        read_only_access=True,
        paper_order_submission=True,
        cancellation=True,
        client_order_id_idempotency=True,
    )
    policy = PaperExecutionPolicy(
        expected_provider="synthetic-paper",
        expected_environment=PaperBrokerEnvironment.SANDBOX,
        expected_account_fingerprint=account_fingerprint,
        expected_endpoint_fingerprint=endpoint_fingerprint,
        currency="USD",
        allowed_symbols=("ABC",),
        maximum_market_age_seconds=60,
        maximum_account_age_seconds=60,
        maximum_order_quantity=100,
        maximum_order_notional=Decimal("10000"),
        maximum_position_quantity=200,
        maximum_open_orders=10,
        minimum_cash_reserve_fraction=Decimal("0.1"),
        maximum_limit_deviation_bps=Decimal("100"),
        cash_reconciliation_tolerance=Decimal("0.01"),
    )
    market = PaperMarketSnapshot(
        symbol="ABC",
        bid=Decimal("99.9"),
        ask=Decimal("100"),
        last=Decimal("99.95"),
        captured_at=now,
        source_digest="c" * 64,
    )
    intent = PaperOrderIntent(
        decision_id="forward-shadow-decision-1",
        symbol="ABC",
        side=PaperOrderSide.BUY,
        quantity=10,
        limit_price=Decimal("100"),
        created_at=now,
        market_snapshot_digest=market.snapshot_digest,
        strategy_id="portfolio-v1",
        reason="synthetic paper safety simulation",
    )
    broker = DeterministicInMemoryPaperBroker(
        profile=profile,
        captured_at=now,
        cash=Decimal("100000"),
    )
    with tempfile.TemporaryDirectory() as directory:
        store = SQLitePaperExecutionStore(Path(directory) / "paper.sqlite3")
        coordinator = PaperOrderCoordinator(
            broker=broker,
            store=store,
            policy=policy,
            clock=FixedPaperExecutionClock(now),
        )
        first = await coordinator.submit(intent, market=market)
        duplicate = await coordinator.submit(intent, market=market)
        broker_snapshot = await broker.get_account_snapshot()
        internal = build_internal_ledger_snapshot(
            store=store,
            account_fingerprint=account_fingerprint,
            captured_at=now,
            cash=broker_snapshot.cash,
            positions=broker_snapshot.positions,
            fill_ids=tuple(fill.fill_id for fill in broker_snapshot.fills),
        )
        reconciliation = DeterministicPaperReconciler().reconcile_and_record(
            internal=internal,
            broker=broker_snapshot,
            policy=policy,
            compared_at=now,
            store=store,
        )

        second_time = now + timedelta(seconds=1)
        broker.set_time(second_time)
        timeout_market = PaperMarketSnapshot(
            symbol="ABC",
            bid=Decimal("100.9"),
            ask=Decimal("101"),
            last=Decimal("100.95"),
            captured_at=second_time,
            source_digest="d" * 64,
        )
        timeout_intent = PaperOrderIntent(
            decision_id="forward-shadow-decision-2",
            symbol="ABC",
            side=PaperOrderSide.BUY,
            quantity=5,
            limit_price=Decimal("101"),
            created_at=second_time,
            market_snapshot_digest=timeout_market.snapshot_digest,
            strategy_id="portfolio-v1",
            reason="synthetic crash-recovery simulation",
        )
        timeout_coordinator = PaperOrderCoordinator(
            broker=broker,
            store=store,
            policy=policy,
            clock=FixedPaperExecutionClock(second_time),
        )
        broker.timeout_after_accept_once()
        timed_out = False
        try:
            await timeout_coordinator.submit(
                timeout_intent,
                market=timeout_market,
            )
        except PaperExecutionSafetyError as error:
            timed_out = "unknown" in str(error).lower()
        kill_switch = store.get_kill_switch().mode
        recovered = await timeout_coordinator.recover_unknown(
            timeout_intent.client_order_id
        )
        digest = sha256_document(
            {
                "first": first.to_document(),
                "duplicate": duplicate.to_document(),
                "reconciliation": reconciliation.to_document(),
                "recovered": recovered.to_document(),
            }
        )
        return PaperExecutionSimulationResult(
            first_status=first.status,
            duplicate_status=duplicate.status,
            broker_submit_calls=broker.submit_calls,
            reconciliation_passed=(
                reconciliation.recommended_kill_switch
                == KillSwitchMode.NORMAL
            ),
            timeout_recovered=(
                timed_out
                and recovered.status == PaperOrderStatus.SUBMITTED
            ),
            kill_switch_after_timeout=kill_switch,
            live_trading_enabled=False,
            deterministic_digest=digest,
        )


def main() -> None:
    first = run_paper_execution_simulation()
    second = run_paper_execution_simulation()
    if first != second:
        raise AssertionError("Paper execution simulation is not deterministic.")
    if first.first_status != PaperOrderStatus.SUBMITTED:
        raise AssertionError("Initial paper order was not submitted.")
    if first.broker_submit_calls != 2:
        raise AssertionError("Unexpected paper broker submission count.")
    if not first.reconciliation_passed:
        raise AssertionError("Matching paper state did not reconcile.")
    if not first.timeout_recovered:
        raise AssertionError("Unknown paper submission was not recovered.")
    if first.kill_switch_after_timeout != KillSwitchMode.SOFT_HALT:
        raise AssertionError("Unknown submission did not trigger a soft halt.")
    if first.live_trading_enabled:
        raise AssertionError("Live trading must remain disabled.")
    print("Broker Paper Execution & Reconciliation v1 simulation passed.")
    print(f"Initial status: {first.first_status.value}")
    print(f"Duplicate status: {first.duplicate_status.value}")
    print(f"Broker submit calls: {first.broker_submit_calls}")
    print("Reconciliation: PASS")
    print("Unknown submission recovery: PASS")
    print(f"Kill switch after timeout: {first.kill_switch_after_timeout.value}")
    print("Broker transport: DETERMINISTIC_IN_MEMORY_ONLY")
    print("Live trading: DISABLED")
    print(f"Deterministic digest: {first.deterministic_digest}")


if __name__ == "__main__":
    main()
