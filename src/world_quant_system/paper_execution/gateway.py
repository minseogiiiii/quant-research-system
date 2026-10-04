from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from world_quant_system.paper_execution.models import (
    BrokerCertificationReport,
    KillSwitchMode,
    OrderEventType,
    PaperAccountSnapshot,
    PaperExecutionIntegrityError,
    PaperExecutionPolicy,
    PaperExecutionSafetyError,
    PaperMarketSnapshot,
    PaperOrderIntent,
    PaperOrderRecord,
    PaperOrderSide,
    PaperOrderStatus,
    PreTradeDecision,
)
from world_quant_system.paper_execution.protocols import PaperOrderBroker
from world_quant_system.paper_execution.store import SQLitePaperExecutionStore

_ZERO = Decimal("0")
_BPS = Decimal("10000")


class PaperExecutionClock(Protocol):
    def now_utc(self) -> datetime:
        """Return the current timezone-aware UTC timestamp."""
        ...


class SystemPaperExecutionClock:
    def now_utc(self) -> datetime:
        return datetime.now(UTC)


class FixedPaperExecutionClock:
    def __init__(self, now: datetime) -> None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise PaperExecutionIntegrityError(
                "Fixed paper-execution clock must be timezone-aware."
            )
        self._now = now.astimezone(UTC)

    def now_utc(self) -> datetime:
        return self._now


class BrokerCertificationService:
    """Certify a paper/sandbox snapshot without mutating broker state."""

    def certify(
        self,
        *,
        account: PaperAccountSnapshot,
        policy: PaperExecutionPolicy,
        evaluated_at: datetime,
        require_order_submission: bool,
    ) -> BrokerCertificationReport:
        reasons: list[str] = []
        profile = account.profile
        if profile.provider != policy.expected_provider:
            reasons.append("broker_provider_mismatch")
        if profile.environment != policy.expected_environment:
            reasons.append("broker_environment_mismatch")
        if profile.account_fingerprint != policy.expected_account_fingerprint:
            reasons.append("account_fingerprint_mismatch")
        if profile.endpoint_fingerprint != policy.expected_endpoint_fingerprint:
            reasons.append("endpoint_fingerprint_mismatch")
        if profile.currency != policy.currency:
            reasons.append("broker_currency_mismatch")
        if account.cash.currency != policy.currency:
            reasons.append("account_currency_mismatch")
        if not profile.read_only_access:
            reasons.append("read_only_account_access_missing")
        if require_order_submission and not profile.paper_order_submission:
            reasons.append("paper_order_submission_capability_missing")
        if require_order_submission and not profile.client_order_id_idempotency:
            reasons.append("client_order_id_idempotency_missing")
        if require_order_submission and not profile.cancellation:
            reasons.append("paper_order_cancellation_capability_missing")
        if profile.margin:
            reasons.append("margin_capability_forbidden")
        if profile.short_selling:
            reasons.append("short_selling_capability_forbidden")
        if profile.fractional_quantity:
            reasons.append("fractional_quantity_capability_forbidden")
        age = evaluated_at.astimezone(UTC) - account.captured_at.astimezone(UTC)
        if age < timedelta(0):
            reasons.append("account_snapshot_from_future")
        if age > timedelta(seconds=policy.maximum_account_age_seconds):
            reasons.append("account_snapshot_stale")
        return BrokerCertificationReport(
            profile_digest=profile.profile_digest,
            policy_digest=policy.policy_digest,
            account_snapshot_digest=account.snapshot_digest,
            certified=not reasons,
            reasons=tuple(sorted(reasons)),
            evaluated_at=evaluated_at,
        )


class PreTradeRiskGateway:
    """Fail-closed paper-order validation with no broker write side effects."""

    def evaluate(
        self,
        *,
        intent: PaperOrderIntent,
        market: PaperMarketSnapshot,
        account: PaperAccountSnapshot,
        policy: PaperExecutionPolicy,
        kill_switch_mode: KillSwitchMode,
        evaluated_at: datetime,
        existing_client_order_ids: frozenset[str],
    ) -> PreTradeDecision:
        reasons: list[str] = []
        profile = account.profile
        if kill_switch_mode != KillSwitchMode.NORMAL:
            reasons.append(f"kill_switch_{kill_switch_mode.value}")
        if profile.provider != policy.expected_provider:
            reasons.append("broker_provider_mismatch")
        if profile.environment != policy.expected_environment:
            reasons.append("broker_environment_mismatch")
        if profile.account_fingerprint != policy.expected_account_fingerprint:
            reasons.append("account_fingerprint_mismatch")
        if profile.endpoint_fingerprint != policy.expected_endpoint_fingerprint:
            reasons.append("endpoint_fingerprint_mismatch")
        if not profile.paper_order_submission:
            reasons.append("paper_order_submission_capability_missing")
        if not profile.client_order_id_idempotency:
            reasons.append("client_order_id_idempotency_missing")
        if profile.margin or profile.short_selling or profile.fractional_quantity:
            reasons.append("forbidden_broker_capability_enabled")
        if intent.client_order_id in existing_client_order_ids:
            reasons.append("duplicate_client_order_id")
        if intent.symbol not in policy.allowed_symbols:
            reasons.append("symbol_not_allowed")
        if market.symbol != intent.symbol:
            reasons.append("market_symbol_mismatch")
        if market.snapshot_digest != intent.market_snapshot_digest:
            reasons.append("market_snapshot_digest_mismatch")
        if account.cash.currency != policy.currency:
            reasons.append("account_currency_mismatch")
        if intent.quantity > policy.maximum_order_quantity:
            reasons.append("maximum_order_quantity_exceeded")
        if intent.notional > policy.maximum_order_notional:
            reasons.append("maximum_order_notional_exceeded")
        if account.open_order_count >= policy.maximum_open_orders:
            reasons.append("maximum_open_orders_reached")
        self._validate_time(
            name="market_snapshot",
            timestamp=market.captured_at,
            evaluated_at=evaluated_at,
            maximum_age_seconds=policy.maximum_market_age_seconds,
            reasons=reasons,
        )
        self._validate_time(
            name="account_snapshot",
            timestamp=account.captured_at,
            evaluated_at=evaluated_at,
            maximum_age_seconds=policy.maximum_account_age_seconds,
            reasons=reasons,
        )
        if intent.created_at > evaluated_at:
            reasons.append("order_intent_from_future")

        current_position = account.positions_by_symbol.get(intent.symbol)
        current_quantity = 0 if current_position is None else current_position.quantity
        if intent.side == PaperOrderSide.BUY:
            projected_position = current_quantity + intent.quantity
            required_cash = intent.notional
            reserve = (
                account.cash.settled_cash
                * policy.minimum_cash_reserve_fraction
            )
            if account.cash.settled_cash - required_cash < reserve:
                reasons.append("insufficient_cash_after_required_reserve")
            reference_price = market.ask
            if policy.require_marketable_limit and intent.limit_price < market.ask:
                reasons.append("buy_limit_not_marketable")
        else:
            projected_position = current_quantity - intent.quantity
            required_cash = _ZERO
            reference_price = market.bid
            if projected_position < 0:
                reasons.append("sell_would_create_short_position")
                projected_position = 0
            if policy.require_marketable_limit and intent.limit_price > market.bid:
                reasons.append("sell_limit_not_marketable")
        if projected_position > policy.maximum_position_quantity:
            reasons.append("maximum_position_quantity_exceeded")
        deviation_bps = (
            abs(intent.limit_price - reference_price)
            / reference_price
            * _BPS
        )
        if deviation_bps > policy.maximum_limit_deviation_bps:
            reasons.append("limit_price_deviation_exceeded")

        return PreTradeDecision(
            intent_id=intent.intent_id,
            client_order_id=intent.client_order_id,
            approved=not reasons,
            reasons=tuple(sorted(set(reasons))),
            evaluated_at=evaluated_at,
            account_snapshot_digest=account.snapshot_digest,
            market_snapshot_digest=market.snapshot_digest,
            policy_digest=policy.policy_digest,
            projected_position_quantity=projected_position,
            required_cash=required_cash,
        )

    @staticmethod
    def _validate_time(
        *,
        name: str,
        timestamp: datetime,
        evaluated_at: datetime,
        maximum_age_seconds: int,
        reasons: list[str],
    ) -> None:
        age = evaluated_at.astimezone(UTC) - timestamp.astimezone(UTC)
        if age < timedelta(0):
            reasons.append(f"{name}_from_future")
        elif age > timedelta(seconds=maximum_age_seconds):
            reasons.append(f"{name}_stale")


class PaperOrderCoordinator:
    """Idempotent paper-only order coordinator with crash recovery."""

    def __init__(
        self,
        *,
        broker: PaperOrderBroker,
        store: SQLitePaperExecutionStore,
        policy: PaperExecutionPolicy,
        clock: PaperExecutionClock | None = None,
    ) -> None:
        self._broker = broker
        self._store = store
        self._policy = policy
        self._clock = clock or SystemPaperExecutionClock()
        self._certifier = BrokerCertificationService()
        self._risk_gateway = PreTradeRiskGateway()

    async def submit(
        self,
        intent: PaperOrderIntent,
        *,
        market: PaperMarketSnapshot,
    ) -> PaperOrderRecord:
        now = self._clock.now_utc()
        record, created = self._store.create_intent(intent, created_at=now)
        if not created:
            recovered = await self._handle_existing(record)
            if recovered is not None:
                return recovered
            record = self._require_order(intent.client_order_id)

        profile = await self._broker.get_capability_profile()
        account = await self._broker.get_account_snapshot()
        if account.profile != profile:
            self._store.activate_kill_switch(
                KillSwitchMode.HARD_HALT,
                reason="broker profile differs between capability and account reads",
                activated_at=now,
            )
            raise PaperExecutionSafetyError(
                "Broker capability profile changed during pre-trade checks."
            )
        certification = self._certifier.certify(
            account=account,
            policy=self._policy,
            evaluated_at=now,
            require_order_submission=True,
        )
        self._store.record_certification(certification)
        if not certification.certified:
            self._store.activate_kill_switch(
                KillSwitchMode.HARD_HALT,
                reason="paper broker certification failed",
                activated_at=now,
            )
            return self._reject(
                record,
                reason="broker certification failed: "
                + ",".join(certification.reasons),
                updated_at=now,
            )

        existing_ids = frozenset(
            item.intent.client_order_id
            for item in self._store.list_orders()
            if item.intent.client_order_id != intent.client_order_id
        )
        decision = self._risk_gateway.evaluate(
            intent=intent,
            market=market,
            account=account,
            policy=self._policy,
            kill_switch_mode=self._store.get_kill_switch().mode,
            evaluated_at=now,
            existing_client_order_ids=existing_ids,
        )
        if not decision.approved:
            return self._reject(
                record,
                reason="pre-trade rejected: " + ",".join(decision.reasons),
                updated_at=now,
            )
        if record.status == PaperOrderStatus.CREATED:
            record = self._store.transition(
                intent.client_order_id,
                target_status=PaperOrderStatus.VALIDATED,
                event_type=OrderEventType.PRETRADE_APPROVED,
                reason="paper pre-trade risk policy approved the intent",
                updated_at=now,
            )
        if record.status == PaperOrderStatus.VALIDATED:
            record = self._store.transition(
                intent.client_order_id,
                target_status=PaperOrderStatus.SUBMITTING,
                event_type=OrderEventType.SUBMISSION_STARTED,
                reason="paper broker submission started after all gates passed",
                updated_at=now,
            )
        if record.status != PaperOrderStatus.SUBMITTING:
            raise PaperExecutionIntegrityError(
                "Paper order is not in a submittable state."
            )

        broker_recovered = await self._broker.find_order_by_client_order_id(
            intent.client_order_id
        )
        if broker_recovered is not None:
            return self._store.apply_broker_order(
                broker_recovered,
                event_type=OrderEventType.SUBMISSION_RECOVERED,
                reason="existing broker order recovered by deterministic client ID",
            )
        try:
            submitted = await self._broker.submit_limit_order(intent)
        except TimeoutError:
            self._store.transition(
                intent.client_order_id,
                target_status=PaperOrderStatus.UNKNOWN,
                event_type=OrderEventType.UNKNOWN_STATE,
                reason="paper broker submission timed out with unknown outcome",
                updated_at=now,
            )
            self._store.activate_kill_switch(
                KillSwitchMode.SOFT_HALT,
                reason="unknown paper order submission outcome",
                activated_at=now,
            )
            raise PaperExecutionSafetyError(
                "Paper order outcome is unknown; reconciliation is required."
            ) from None
        event_type = (
            OrderEventType.BROKER_REJECTED
            if submitted.status == PaperOrderStatus.REJECTED
            else OrderEventType.SUBMISSION_CONFIRMED
        )
        return self._store.apply_broker_order(
            submitted,
            event_type=event_type,
            reason="paper broker acknowledged deterministic client order ID",
        )

    async def recover_unknown(self, client_order_id: str) -> PaperOrderRecord:
        record = self._require_order(client_order_id)
        if record.status not in {
            PaperOrderStatus.SUBMITTING,
            PaperOrderStatus.UNKNOWN,
        }:
            return record
        broker_order = await self._broker.find_order_by_client_order_id(
            client_order_id
        )
        if broker_order is None:
            self._store.activate_kill_switch(
                KillSwitchMode.SOFT_HALT,
                reason="unknown paper order not found during recovery",
                activated_at=self._clock.now_utc(),
            )
            raise PaperExecutionSafetyError(
                "Unknown paper order was not found; human review is required."
            )
        return self._store.apply_broker_order(
            broker_order,
            event_type=OrderEventType.SUBMISSION_RECOVERED,
            reason="paper order state recovered by deterministic client ID",
        )

    async def cancel(self, client_order_id: str) -> PaperOrderRecord:
        now = self._clock.now_utc()
        kill_switch = self._store.get_kill_switch()
        if kill_switch.mode not in {
            KillSwitchMode.NORMAL,
            KillSwitchMode.CANCEL_ONLY,
        }:
            raise PaperExecutionSafetyError(
                "Current kill-switch mode does not permit cancellation."
            )
        record = self._require_order(client_order_id)
        if record.broker_order_id is None:
            raise PaperExecutionIntegrityError(
                "Cannot cancel an order without a broker order ID."
            )
        if record.status not in {
            PaperOrderStatus.SUBMITTED,
            PaperOrderStatus.PARTIALLY_FILLED,
        }:
            return record
        profile = await self._broker.get_capability_profile()
        if not profile.cancellation:
            raise PaperExecutionSafetyError(
                "Certified paper broker does not support cancellation."
            )
        self._store.transition(
            client_order_id,
            target_status=PaperOrderStatus.CANCEL_PENDING,
            event_type=OrderEventType.CANCEL_REQUESTED,
            reason="paper order cancellation requested",
            updated_at=now,
        )
        canceled = await self._broker.cancel_order(record.broker_order_id)
        return self._store.apply_broker_order(
            canceled,
            event_type=OrderEventType.CANCELED,
            reason="paper broker confirmed cancellation",
        )

    async def _handle_existing(
        self,
        record: PaperOrderRecord,
    ) -> PaperOrderRecord | None:
        if record.status in {
            PaperOrderStatus.SUBMITTED,
            PaperOrderStatus.PARTIALLY_FILLED,
            PaperOrderStatus.FILLED,
            PaperOrderStatus.CANCEL_PENDING,
            PaperOrderStatus.CANCELED,
            PaperOrderStatus.REJECTED,
        }:
            return record
        if record.status in {
            PaperOrderStatus.SUBMITTING,
            PaperOrderStatus.UNKNOWN,
        }:
            return await self.recover_unknown(record.intent.client_order_id)
        return None

    def _reject(
        self,
        record: PaperOrderRecord,
        *,
        reason: str,
        updated_at: datetime,
    ) -> PaperOrderRecord:
        if record.status == PaperOrderStatus.REJECTED:
            return record
        event_type = (
            OrderEventType.PRETRADE_REJECTED
            if record.status in {
                PaperOrderStatus.CREATED,
                PaperOrderStatus.VALIDATED,
            }
            else OrderEventType.BROKER_REJECTED
        )
        return self._store.transition(
            record.intent.client_order_id,
            target_status=PaperOrderStatus.REJECTED,
            event_type=event_type,
            reason=reason,
            updated_at=updated_at,
            rejection_reason=reason,
        )

    def _require_order(self, client_order_id: str) -> PaperOrderRecord:
        record = self._store.get_order(client_order_id)
        if record is None:
            raise PaperExecutionIntegrityError(
                f"Unknown paper order: {client_order_id}."
            )
        return record
