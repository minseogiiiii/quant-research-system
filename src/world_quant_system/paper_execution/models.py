from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid5

_SCHEMA_VERSION: Final[int] = 1
_REPORT_NAMESPACE: Final[UUID] = UUID("56c4f750-7658-5a21-865d-c9191f1b8fa4")
_ZERO = Decimal("0")
_ONE = Decimal("1")


class PaperExecutionError(Exception):
    """Base exception for paper-execution safety failures."""


class PaperExecutionConfigurationError(PaperExecutionError):
    """Raised when configuration or input is invalid."""


class PaperExecutionIntegrityError(PaperExecutionError):
    """Raised when append-only execution state is inconsistent."""


class PaperExecutionSafetyError(PaperExecutionError):
    """Raised when a fail-closed safety gate rejects an operation."""


class PaperBrokerEnvironment(StrEnum):
    SANDBOX = "sandbox"
    PAPER = "paper"


class PaperOrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class PaperOrderType(StrEnum):
    LIMIT = "limit"


class PaperOrderStatus(StrEnum):
    CREATED = "created"
    VALIDATED = "validated"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELED = "canceled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class KillSwitchMode(StrEnum):
    NORMAL = "normal"
    SOFT_HALT = "soft_halt"
    CANCEL_ONLY = "cancel_only"
    HARD_HALT = "hard_halt"


class DiscrepancySeverity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


class ReconciliationDecision(StrEnum):
    PASS = "pass"
    HALT = "halt"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"


class OrderEventType(StrEnum):
    INTENT_CREATED = "intent_created"
    PRETRADE_APPROVED = "pretrade_approved"
    PRETRADE_REJECTED = "pretrade_rejected"
    SUBMISSION_STARTED = "submission_started"
    SUBMISSION_CONFIRMED = "submission_confirmed"
    SUBMISSION_RECOVERED = "submission_recovered"
    BROKER_REJECTED = "broker_rejected"
    PARTIAL_FILL_RECORDED = "partial_fill_recorded"
    FILL_RECORDED = "fill_recorded"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELED = "canceled"
    UNKNOWN_STATE = "unknown_state"
    IDEMPOTENT_REPLAY = "idempotent_replay"


_TERMINAL_ORDER_STATUSES: Final[frozenset[PaperOrderStatus]] = frozenset(
    {
        PaperOrderStatus.FILLED,
        PaperOrderStatus.CANCELED,
        PaperOrderStatus.REJECTED,
    }
)

_ALLOWED_TRANSITIONS: Final[dict[PaperOrderStatus, frozenset[PaperOrderStatus]]] = {
    PaperOrderStatus.CREATED: frozenset(
        {
            PaperOrderStatus.VALIDATED,
            PaperOrderStatus.REJECTED,
        }
    ),
    PaperOrderStatus.VALIDATED: frozenset(
        {
            PaperOrderStatus.SUBMITTING,
            PaperOrderStatus.REJECTED,
        }
    ),
    PaperOrderStatus.SUBMITTING: frozenset(
        {
            PaperOrderStatus.SUBMITTED,
            PaperOrderStatus.PARTIALLY_FILLED,
            PaperOrderStatus.FILLED,
            PaperOrderStatus.REJECTED,
            PaperOrderStatus.UNKNOWN,
        }
    ),
    PaperOrderStatus.SUBMITTED: frozenset(
        {
            PaperOrderStatus.PARTIALLY_FILLED,
            PaperOrderStatus.FILLED,
            PaperOrderStatus.CANCEL_PENDING,
            PaperOrderStatus.CANCELED,
            PaperOrderStatus.REJECTED,
            PaperOrderStatus.UNKNOWN,
        }
    ),
    PaperOrderStatus.PARTIALLY_FILLED: frozenset(
        {
            PaperOrderStatus.PARTIALLY_FILLED,
            PaperOrderStatus.FILLED,
            PaperOrderStatus.CANCEL_PENDING,
            PaperOrderStatus.CANCELED,
            PaperOrderStatus.UNKNOWN,
        }
    ),
    PaperOrderStatus.CANCEL_PENDING: frozenset(
        {
            PaperOrderStatus.CANCELED,
            PaperOrderStatus.PARTIALLY_FILLED,
            PaperOrderStatus.FILLED,
            PaperOrderStatus.UNKNOWN,
        }
    ),
    PaperOrderStatus.UNKNOWN: frozenset(
        {
            PaperOrderStatus.SUBMITTED,
            PaperOrderStatus.PARTIALLY_FILLED,
            PaperOrderStatus.FILLED,
            PaperOrderStatus.CANCELED,
            PaperOrderStatus.REJECTED,
        }
    ),
    PaperOrderStatus.FILLED: frozenset(),
    PaperOrderStatus.CANCELED: frozenset(),
    PaperOrderStatus.REJECTED: frozenset(),
}


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def format_utc(value: datetime) -> str:
    _require_aware(value, "Timestamp")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_utc_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PaperExecutionConfigurationError(
            f"{field_name} must be an ISO-8601 timestamp."
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PaperExecutionConfigurationError(
            f"{field_name} must be an ISO-8601 timestamp."
        ) from error
    _require_aware(parsed, field_name)
    return parsed.astimezone(UTC)


def decimal_from(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise PaperExecutionConfigurationError(
            f"{field_name} must be a decimal value."
        )
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise PaperExecutionConfigurationError(
            f"{field_name} must be a decimal value."
        ) from error
    _require_finite_decimal(parsed, field_name)
    return parsed


def sha256_document(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def is_terminal_status(status: PaperOrderStatus) -> bool:
    return status in _TERMINAL_ORDER_STATUSES


def validate_order_transition(
    current: PaperOrderStatus,
    target: PaperOrderStatus,
) -> None:
    if not isinstance(current, PaperOrderStatus):
        raise PaperExecutionConfigurationError("Current order status is invalid.")
    if not isinstance(target, PaperOrderStatus):
        raise PaperExecutionConfigurationError("Target order status is invalid.")
    if target == current and target == PaperOrderStatus.PARTIALLY_FILLED:
        return
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise PaperExecutionIntegrityError(
            f"Order status transition {current.value} -> {target.value} is invalid."
        )


@dataclass(frozen=True, slots=True)
class BrokerCapabilityProfile:
    provider: str
    environment: PaperBrokerEnvironment
    account_fingerprint: str
    endpoint_fingerprint: str
    currency: str
    read_only_access: bool
    paper_order_submission: bool
    cancellation: bool
    client_order_id_idempotency: bool
    margin: bool = False
    short_selling: bool = False
    fractional_quantity: bool = False

    def __post_init__(self) -> None:
        _require_nonblank(self.provider, "Broker provider")
        if not isinstance(self.environment, PaperBrokerEnvironment):
            raise PaperExecutionConfigurationError(
                "Broker environment must be sandbox or paper."
            )
        _require_sha256(self.account_fingerprint, "Account fingerprint")
        _require_sha256(self.endpoint_fingerprint, "Endpoint fingerprint")
        _require_currency(self.currency)
        for boolean_value, field_name in (
            (self.read_only_access, "Read-only access"),
            (self.paper_order_submission, "Paper order submission"),
            (self.cancellation, "Cancellation capability"),
            (
                self.client_order_id_idempotency,
                "Client-order-ID idempotency",
            ),
            (self.margin, "Margin capability"),
            (self.short_selling, "Short-selling capability"),
            (self.fractional_quantity, "Fractional-quantity capability"),
        ):
            _require_bool(boolean_value, field_name)
        if not self.read_only_access:
            raise PaperExecutionSafetyError(
                "Paper broker certification requires read-only account access."
            )
        if self.margin or self.short_selling or self.fractional_quantity:
            raise PaperExecutionSafetyError(
                "Paper v1 forbids margin, short selling, and fractional quantities."
            )

    @property
    def profile_digest(self) -> str:
        return sha256_document(self.to_document())

    def to_document(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "environment": self.environment.value,
            "account_fingerprint": self.account_fingerprint,
            "endpoint_fingerprint": self.endpoint_fingerprint,
            "currency": self.currency,
            "read_only_access": self.read_only_access,
            "paper_order_submission": self.paper_order_submission,
            "cancellation": self.cancellation,
            "client_order_id_idempotency": (
                self.client_order_id_idempotency
            ),
            "margin": self.margin,
            "short_selling": self.short_selling,
            "fractional_quantity": self.fractional_quantity,
        }


@dataclass(frozen=True, slots=True)
class PaperCashBalance:
    currency: str
    settled_cash: Decimal
    buying_power: Decimal

    def __post_init__(self) -> None:
        _require_currency(self.currency)
        _require_nonnegative_decimal(self.settled_cash, "Settled cash")
        _require_nonnegative_decimal(self.buying_power, "Buying power")
        if self.buying_power < self.settled_cash:
            raise PaperExecutionSafetyError(
                "Buying power cannot be below settled cash in no-margin paper v1."
            )
        if self.buying_power != self.settled_cash:
            raise PaperExecutionSafetyError(
                "Paper v1 forbids margin-derived buying power."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "currency": self.currency,
            "settled_cash": _decimal_text(self.settled_cash),
            "buying_power": _decimal_text(self.buying_power),
        }


@dataclass(frozen=True, slots=True)
class PaperPosition:
    symbol: str
    quantity: int
    average_price: Decimal

    def __post_init__(self) -> None:
        _require_symbol(self.symbol)
        _require_nonnegative_int(self.quantity, "Position quantity")
        _require_nonnegative_decimal(self.average_price, "Average price")
        if self.quantity > 0 and self.average_price <= _ZERO:
            raise PaperExecutionConfigurationError(
                "Nonzero positions require a positive average price."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "average_price": _decimal_text(self.average_price),
        }


@dataclass(frozen=True, slots=True)
class PaperMarketSnapshot:
    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    captured_at: datetime
    source_digest: str
    source: str = "offline_snapshot"

    def __post_init__(self) -> None:
        _require_symbol(self.symbol)
        for price_value, field_name in (
            (self.bid, "Bid"),
            (self.ask, "Ask"),
            (self.last, "Last price"),
        ):
            _require_positive_decimal(price_value, field_name)
        if self.ask < self.bid:
            raise PaperExecutionIntegrityError("Ask price cannot be below bid price.")
        _require_aware(self.captured_at, "Market snapshot time")
        _require_sha256(self.source_digest, "Market source digest")
        _require_nonblank(self.source, "Market source")

    @property
    def snapshot_digest(self) -> str:
        return sha256_document(self.to_document())

    def to_document(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "bid": _decimal_text(self.bid),
            "ask": _decimal_text(self.ask),
            "last": _decimal_text(self.last),
            "captured_at": format_utc(self.captured_at),
            "source_digest": self.source_digest,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class PaperOrderIntent:
    decision_id: str
    symbol: str
    side: PaperOrderSide
    quantity: int
    limit_price: Decimal
    created_at: datetime
    market_snapshot_digest: str
    strategy_id: str
    reason: str
    order_type: PaperOrderType = PaperOrderType.LIMIT
    intent_id: str = field(init=False)
    client_order_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_nonblank(self.decision_id, "Decision ID")
        _require_symbol(self.symbol)
        if not isinstance(self.side, PaperOrderSide):
            raise PaperExecutionConfigurationError("Order side is invalid.")
        _require_positive_int(self.quantity, "Order quantity")
        _require_positive_decimal(self.limit_price, "Limit price")
        _require_aware(self.created_at, "Order intent time")
        _require_sha256(
            self.market_snapshot_digest,
            "Market snapshot digest",
        )
        _require_nonblank(self.strategy_id, "Strategy ID")
        _require_nonblank(self.reason, "Order reason")
        if self.order_type != PaperOrderType.LIMIT:
            raise PaperExecutionSafetyError("Paper v1 permits limit orders only.")
        identity = {
            "decision_id": self.decision_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": self.quantity,
            "limit_price": _decimal_text(self.limit_price),
            "created_at": format_utc(self.created_at),
            "market_snapshot_digest": self.market_snapshot_digest,
            "strategy_id": self.strategy_id,
            "reason": self.reason,
            "order_type": self.order_type.value,
        }
        digest = sha256_document(identity)
        object.__setattr__(self, "intent_id", digest)
        object.__setattr__(self, "client_order_id", f"wqs-{digest[:28]}")

    @property
    def notional(self) -> Decimal:
        return self.limit_price * Decimal(self.quantity)

    def to_document(self) -> dict[str, object]:
        return {
            "intent_id": self.intent_id,
            "client_order_id": self.client_order_id,
            "decision_id": self.decision_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": self.quantity,
            "limit_price": _decimal_text(self.limit_price),
            "created_at": format_utc(self.created_at),
            "market_snapshot_digest": self.market_snapshot_digest,
            "strategy_id": self.strategy_id,
            "reason": self.reason,
            "order_type": self.order_type.value,
        }


@dataclass(frozen=True, slots=True)
class BrokerOrderSnapshot:
    client_order_id: str
    broker_order_id: str
    symbol: str
    side: PaperOrderSide
    quantity: int
    limit_price: Decimal
    filled_quantity: int
    average_fill_price: Decimal | None
    status: PaperOrderStatus
    submitted_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_client_order_id(self.client_order_id)
        _require_nonblank(self.broker_order_id, "Broker order ID")
        _require_symbol(self.symbol)
        if not isinstance(self.side, PaperOrderSide):
            raise PaperExecutionConfigurationError("Broker order side is invalid.")
        _require_positive_int(self.quantity, "Broker order quantity")
        _require_positive_decimal(self.limit_price, "Broker limit price")
        _require_nonnegative_int(self.filled_quantity, "Filled quantity")
        if self.filled_quantity > self.quantity:
            raise PaperExecutionIntegrityError(
                "Filled quantity cannot exceed requested quantity."
            )
        if self.average_fill_price is not None:
            _require_positive_decimal(
                self.average_fill_price,
                "Average fill price",
            )
        if self.filled_quantity > 0 and self.average_fill_price is None:
            raise PaperExecutionIntegrityError(
                "Filled orders require an average fill price."
            )
        if not isinstance(self.status, PaperOrderStatus):
            raise PaperExecutionConfigurationError("Broker order status is invalid.")
        _require_aware(self.submitted_at, "Broker submission time")
        _require_aware(self.updated_at, "Broker order update time")
        if self.updated_at < self.submitted_at:
            raise PaperExecutionIntegrityError(
                "Broker order update cannot precede submission."
            )
        _validate_status_quantities(
            self.status,
            self.quantity,
            self.filled_quantity,
        )

    def to_document(self) -> dict[str, object]:
        return {
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": self.quantity,
            "limit_price": _decimal_text(self.limit_price),
            "filled_quantity": self.filled_quantity,
            "average_fill_price": (
                None
                if self.average_fill_price is None
                else _decimal_text(self.average_fill_price)
            ),
            "status": self.status.value,
            "submitted_at": format_utc(self.submitted_at),
            "updated_at": format_utc(self.updated_at),
        }


@dataclass(frozen=True, slots=True)
class PaperFillSnapshot:
    fill_id: str
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: PaperOrderSide
    quantity: int
    price: Decimal
    commission: Decimal
    filled_at: datetime

    def __post_init__(self) -> None:
        _require_nonblank(self.fill_id, "Fill ID")
        _require_nonblank(self.broker_order_id, "Broker order ID")
        _require_client_order_id(self.client_order_id)
        _require_symbol(self.symbol)
        if not isinstance(self.side, PaperOrderSide):
            raise PaperExecutionConfigurationError("Fill side is invalid.")
        _require_positive_int(self.quantity, "Fill quantity")
        _require_positive_decimal(self.price, "Fill price")
        _require_nonnegative_decimal(self.commission, "Fill commission")
        _require_aware(self.filled_at, "Fill time")

    def to_document(self) -> dict[str, object]:
        return {
            "fill_id": self.fill_id,
            "broker_order_id": self.broker_order_id,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": self.quantity,
            "price": _decimal_text(self.price),
            "commission": _decimal_text(self.commission),
            "filled_at": format_utc(self.filled_at),
        }


@dataclass(frozen=True, slots=True)
class PaperAccountSnapshot:
    profile: BrokerCapabilityProfile
    captured_at: datetime
    cash: PaperCashBalance
    positions: tuple[PaperPosition, ...]
    orders: tuple[BrokerOrderSnapshot, ...]
    fills: tuple[PaperFillSnapshot, ...]
    source_digest: str
    snapshot_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.profile, BrokerCapabilityProfile):
            raise PaperExecutionConfigurationError(
                "Account snapshot requires a capability profile."
            )
        _require_aware(self.captured_at, "Account snapshot time")
        if not isinstance(self.cash, PaperCashBalance):
            raise PaperExecutionConfigurationError(
                "Account snapshot cash balance is invalid."
            )
        if self.cash.currency != self.profile.currency:
            raise PaperExecutionIntegrityError(
                "Account cash currency differs from broker profile."
            )
        _require_sorted_unique_positions(self.positions)
        _require_sorted_unique_orders(self.orders)
        _require_sorted_unique_fills(self.fills)
        _require_sha256(self.source_digest, "Account source digest")
        object.__setattr__(
            self,
            "snapshot_digest",
            sha256_document(self._identity_document()),
        )

    @property
    def positions_by_symbol(self) -> dict[str, PaperPosition]:
        return {item.symbol: item for item in self.positions}

    @property
    def orders_by_client_id(self) -> dict[str, BrokerOrderSnapshot]:
        return {item.client_order_id: item for item in self.orders}

    @property
    def fills_by_id(self) -> dict[str, PaperFillSnapshot]:
        return {item.fill_id: item for item in self.fills}

    @property
    def open_order_count(self) -> int:
        return sum(1 for item in self.orders if not is_terminal_status(item.status))

    def _identity_document(self) -> dict[str, object]:
        return {
            "profile": self.profile.to_document(),
            "captured_at": format_utc(self.captured_at),
            "cash": self.cash.to_document(),
            "positions": [item.to_document() for item in self.positions],
            "orders": [item.to_document() for item in self.orders],
            "fills": [item.to_document() for item in self.fills],
            "source_digest": self.source_digest,
        }

    def to_document(self) -> dict[str, object]:
        return {
            **self._identity_document(),
            "snapshot_digest": self.snapshot_digest,
        }


@dataclass(frozen=True, slots=True)
class PaperExecutionPolicy:
    expected_provider: str
    expected_environment: PaperBrokerEnvironment
    expected_account_fingerprint: str
    expected_endpoint_fingerprint: str
    currency: str
    allowed_symbols: tuple[str, ...]
    maximum_market_age_seconds: int
    maximum_account_age_seconds: int
    maximum_order_quantity: int
    maximum_order_notional: Decimal
    maximum_position_quantity: int
    maximum_open_orders: int
    minimum_cash_reserve_fraction: Decimal
    maximum_limit_deviation_bps: Decimal
    cash_reconciliation_tolerance: Decimal
    require_marketable_limit: bool = True

    def __post_init__(self) -> None:
        _require_nonblank(self.expected_provider, "Expected provider")
        if not isinstance(self.expected_environment, PaperBrokerEnvironment):
            raise PaperExecutionConfigurationError(
                "Expected environment must be sandbox or paper."
            )
        _require_sha256(
            self.expected_account_fingerprint,
            "Expected account fingerprint",
        )
        _require_sha256(
            self.expected_endpoint_fingerprint,
            "Expected endpoint fingerprint",
        )
        _require_currency(self.currency)
        if not self.allowed_symbols:
            raise PaperExecutionConfigurationError(
                "Paper policy requires at least one allowed symbol."
            )
        if self.allowed_symbols != tuple(sorted(self.allowed_symbols)):
            raise PaperExecutionConfigurationError(
                "Allowed symbols must be sorted."
            )
        if len(set(self.allowed_symbols)) != len(self.allowed_symbols):
            raise PaperExecutionConfigurationError(
                "Allowed symbols cannot contain duplicates."
            )
        for symbol in self.allowed_symbols:
            _require_symbol(symbol)
        _require_positive_int(
            self.maximum_market_age_seconds,
            "Maximum market age",
        )
        _require_positive_int(
            self.maximum_account_age_seconds,
            "Maximum account age",
        )
        _require_positive_int(
            self.maximum_order_quantity,
            "Maximum order quantity",
        )
        _require_positive_decimal(
            self.maximum_order_notional,
            "Maximum order notional",
        )
        _require_positive_int(
            self.maximum_position_quantity,
            "Maximum position quantity",
        )
        _require_positive_int(self.maximum_open_orders, "Maximum open orders")
        _require_unit_interval(
            self.minimum_cash_reserve_fraction,
            "Minimum cash reserve fraction",
            include_one=False,
        )
        _require_nonnegative_decimal(
            self.maximum_limit_deviation_bps,
            "Maximum limit deviation bps",
        )
        _require_nonnegative_decimal(
            self.cash_reconciliation_tolerance,
            "Cash reconciliation tolerance",
        )
        _require_bool(self.require_marketable_limit, "Marketable-limit requirement")

    @property
    def policy_digest(self) -> str:
        return sha256_document(self.to_document())

    def to_document(self) -> dict[str, object]:
        return {
            "expected_provider": self.expected_provider,
            "expected_environment": self.expected_environment.value,
            "expected_account_fingerprint": self.expected_account_fingerprint,
            "expected_endpoint_fingerprint": self.expected_endpoint_fingerprint,
            "currency": self.currency,
            "allowed_symbols": list(self.allowed_symbols),
            "maximum_market_age_seconds": self.maximum_market_age_seconds,
            "maximum_account_age_seconds": self.maximum_account_age_seconds,
            "maximum_order_quantity": self.maximum_order_quantity,
            "maximum_order_notional": _decimal_text(
                self.maximum_order_notional
            ),
            "maximum_position_quantity": self.maximum_position_quantity,
            "maximum_open_orders": self.maximum_open_orders,
            "minimum_cash_reserve_fraction": _decimal_text(
                self.minimum_cash_reserve_fraction
            ),
            "maximum_limit_deviation_bps": _decimal_text(
                self.maximum_limit_deviation_bps
            ),
            "cash_reconciliation_tolerance": _decimal_text(
                self.cash_reconciliation_tolerance
            ),
            "require_marketable_limit": self.require_marketable_limit,
        }


@dataclass(frozen=True, slots=True)
class KillSwitchState:
    mode: KillSwitchMode
    reason: str
    activated_at: datetime | None
    version: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.mode, KillSwitchMode):
            raise PaperExecutionConfigurationError("Kill-switch mode is invalid.")
        _require_nonnegative_int(self.version, "Kill-switch version")
        if self.mode == KillSwitchMode.NORMAL:
            if self.activated_at is not None:
                raise PaperExecutionConfigurationError(
                    "Normal kill-switch state cannot have an activation time."
                )
            if self.reason not in {"", "normal"}:
                raise PaperExecutionConfigurationError(
                    "Normal kill-switch state cannot carry a halt reason."
                )
        else:
            _require_nonblank(self.reason, "Kill-switch reason")
            if self.activated_at is None:
                raise PaperExecutionConfigurationError(
                    "Halted kill-switch states require an activation time."
                )
            _require_aware(self.activated_at, "Kill-switch activation time")

    def to_document(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "reason": self.reason,
            "activated_at": (
                None
                if self.activated_at is None
                else format_utc(self.activated_at)
            ),
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class PreTradeDecision:
    intent_id: str
    client_order_id: str
    approved: bool
    reasons: tuple[str, ...]
    evaluated_at: datetime
    account_snapshot_digest: str
    market_snapshot_digest: str
    policy_digest: str
    projected_position_quantity: int
    required_cash: Decimal
    decision_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.intent_id, "Intent ID")
        _require_client_order_id(self.client_order_id)
        _require_bool(self.approved, "Pre-trade approval")
        if self.approved and self.reasons:
            raise PaperExecutionIntegrityError(
                "Approved pre-trade decisions cannot contain rejection reasons."
            )
        if not self.approved and not self.reasons:
            raise PaperExecutionIntegrityError(
                "Rejected pre-trade decisions require reasons."
            )
        for reason in self.reasons:
            _require_nonblank(reason, "Pre-trade reason")
        _require_aware(self.evaluated_at, "Pre-trade evaluation time")
        _require_sha256(
            self.account_snapshot_digest,
            "Account snapshot digest",
        )
        _require_sha256(
            self.market_snapshot_digest,
            "Market snapshot digest",
        )
        _require_sha256(self.policy_digest, "Policy digest")
        _require_nonnegative_int(
            self.projected_position_quantity,
            "Projected position quantity",
        )
        _require_nonnegative_decimal(self.required_cash, "Required cash")
        object.__setattr__(
            self,
            "decision_digest",
            sha256_document(self._identity_document()),
        )

    def _identity_document(self) -> dict[str, object]:
        return {
            "intent_id": self.intent_id,
            "client_order_id": self.client_order_id,
            "approved": self.approved,
            "reasons": list(self.reasons),
            "evaluated_at": format_utc(self.evaluated_at),
            "account_snapshot_digest": self.account_snapshot_digest,
            "market_snapshot_digest": self.market_snapshot_digest,
            "policy_digest": self.policy_digest,
            "projected_position_quantity": self.projected_position_quantity,
            "required_cash": _decimal_text(self.required_cash),
        }

    def to_document(self) -> dict[str, object]:
        return {
            **self._identity_document(),
            "decision_digest": self.decision_digest,
        }


@dataclass(frozen=True, slots=True)
class PaperOrderRecord:
    intent: PaperOrderIntent
    status: PaperOrderStatus
    broker_order_id: str | None
    filled_quantity: int
    average_fill_price: Decimal | None
    rejection_reason: str | None
    created_at: datetime
    updated_at: datetime
    version: int
    last_event_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.intent, PaperOrderIntent):
            raise PaperExecutionConfigurationError("Order record intent is invalid.")
        if not isinstance(self.status, PaperOrderStatus):
            raise PaperExecutionConfigurationError("Order record status is invalid.")
        if self.broker_order_id is not None:
            _require_nonblank(self.broker_order_id, "Broker order ID")
        _require_nonnegative_int(self.filled_quantity, "Filled quantity")
        if self.filled_quantity > self.intent.quantity:
            raise PaperExecutionIntegrityError(
                "Record filled quantity exceeds order quantity."
            )
        if self.average_fill_price is not None:
            _require_positive_decimal(
                self.average_fill_price,
                "Average fill price",
            )
        if self.filled_quantity > 0 and self.average_fill_price is None:
            raise PaperExecutionIntegrityError(
                "Filled records require an average fill price."
            )
        if self.rejection_reason is not None:
            _require_nonblank(self.rejection_reason, "Rejection reason")
        _require_aware(self.created_at, "Order record creation time")
        _require_aware(self.updated_at, "Order record update time")
        if self.updated_at < self.created_at:
            raise PaperExecutionIntegrityError(
                "Order record update cannot precede creation."
            )
        _require_nonnegative_int(self.version, "Order record version")
        _require_sha256(self.last_event_hash, "Last event hash")
        _validate_status_quantities(
            self.status,
            self.intent.quantity,
            self.filled_quantity,
        )
        if self.status in {
            PaperOrderStatus.SUBMITTED,
            PaperOrderStatus.PARTIALLY_FILLED,
            PaperOrderStatus.FILLED,
            PaperOrderStatus.CANCEL_PENDING,
            PaperOrderStatus.CANCELED,
        } and self.broker_order_id is None:
            raise PaperExecutionIntegrityError(
                "Submitted or filled records require a broker order ID."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "intent": self.intent.to_document(),
            "status": self.status.value,
            "broker_order_id": self.broker_order_id,
            "filled_quantity": self.filled_quantity,
            "average_fill_price": (
                None
                if self.average_fill_price is None
                else _decimal_text(self.average_fill_price)
            ),
            "rejection_reason": self.rejection_reason,
            "created_at": format_utc(self.created_at),
            "updated_at": format_utc(self.updated_at),
            "version": self.version,
            "last_event_hash": self.last_event_hash,
        }


@dataclass(frozen=True, slots=True)
class PaperOrderEvent:
    sequence: int
    client_order_id: str
    timestamp: datetime
    event_type: OrderEventType
    previous_status: PaperOrderStatus | None
    new_status: PaperOrderStatus
    reason: str
    broker_order_id: str | None
    previous_hash: str
    event_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_positive_int(self.sequence, "Order event sequence")
        _require_client_order_id(self.client_order_id)
        _require_aware(self.timestamp, "Order event time")
        if not isinstance(self.event_type, OrderEventType):
            raise PaperExecutionConfigurationError("Order event type is invalid.")
        if self.previous_status is not None and not isinstance(
            self.previous_status,
            PaperOrderStatus,
        ):
            raise PaperExecutionConfigurationError(
                "Previous order status is invalid."
            )
        if not isinstance(self.new_status, PaperOrderStatus):
            raise PaperExecutionConfigurationError("New order status is invalid.")
        if self.previous_status is not None:
            validate_order_transition(self.previous_status, self.new_status)
        _require_nonblank(self.reason, "Order event reason")
        if self.broker_order_id is not None:
            _require_nonblank(self.broker_order_id, "Broker order ID")
        _require_sha256(self.previous_hash, "Previous event hash")
        object.__setattr__(
            self,
            "event_hash",
            sha256_document(self._identity_document()),
        )

    def _identity_document(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "client_order_id": self.client_order_id,
            "timestamp": format_utc(self.timestamp),
            "event_type": self.event_type.value,
            "previous_status": (
                None
                if self.previous_status is None
                else self.previous_status.value
            ),
            "new_status": self.new_status.value,
            "reason": self.reason,
            "broker_order_id": self.broker_order_id,
            "previous_hash": self.previous_hash,
        }

    def to_document(self) -> dict[str, object]:
        return {
            **self._identity_document(),
            "event_hash": self.event_hash,
        }


@dataclass(frozen=True, slots=True)
class InternalOrderState:
    client_order_id: str
    broker_order_id: str | None
    status: PaperOrderStatus
    filled_quantity: int

    def __post_init__(self) -> None:
        _require_client_order_id(self.client_order_id)
        if self.broker_order_id is not None:
            _require_nonblank(self.broker_order_id, "Broker order ID")
        if not isinstance(self.status, PaperOrderStatus):
            raise PaperExecutionConfigurationError("Internal order status is invalid.")
        _require_nonnegative_int(self.filled_quantity, "Filled quantity")

    def to_document(self) -> dict[str, object]:
        return {
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "status": self.status.value,
            "filled_quantity": self.filled_quantity,
        }


@dataclass(frozen=True, slots=True)
class InternalPaperLedgerSnapshot:
    account_fingerprint: str
    captured_at: datetime
    cash: PaperCashBalance
    positions: tuple[PaperPosition, ...]
    orders: tuple[InternalOrderState, ...]
    fill_ids: tuple[str, ...]
    snapshot_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.account_fingerprint, "Account fingerprint")
        _require_aware(self.captured_at, "Internal ledger time")
        if not isinstance(self.cash, PaperCashBalance):
            raise PaperExecutionConfigurationError("Internal cash is invalid.")
        _require_sorted_unique_positions(self.positions)
        client_ids = tuple(item.client_order_id for item in self.orders)
        if client_ids != tuple(sorted(client_ids)):
            raise PaperExecutionConfigurationError(
                "Internal orders must be sorted by client order ID."
            )
        if len(set(client_ids)) != len(client_ids):
            raise PaperExecutionIntegrityError(
                "Internal orders cannot repeat a client order ID."
            )
        if self.fill_ids != tuple(sorted(self.fill_ids)):
            raise PaperExecutionConfigurationError("Fill IDs must be sorted.")
        if len(set(self.fill_ids)) != len(self.fill_ids):
            raise PaperExecutionIntegrityError("Fill IDs cannot contain duplicates.")
        for fill_id in self.fill_ids:
            _require_nonblank(fill_id, "Fill ID")
        object.__setattr__(
            self,
            "snapshot_digest",
            sha256_document(self._identity_document()),
        )

    @property
    def positions_by_symbol(self) -> dict[str, PaperPosition]:
        return {item.symbol: item for item in self.positions}

    @property
    def orders_by_client_id(self) -> dict[str, InternalOrderState]:
        return {item.client_order_id: item for item in self.orders}

    def _identity_document(self) -> dict[str, object]:
        return {
            "account_fingerprint": self.account_fingerprint,
            "captured_at": format_utc(self.captured_at),
            "cash": self.cash.to_document(),
            "positions": [item.to_document() for item in self.positions],
            "orders": [item.to_document() for item in self.orders],
            "fill_ids": list(self.fill_ids),
        }

    def to_document(self) -> dict[str, object]:
        return {
            **self._identity_document(),
            "snapshot_digest": self.snapshot_digest,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationDiscrepancy:
    code: str
    severity: DiscrepancySeverity
    message: str
    symbol: str | None = None
    client_order_id: str | None = None

    def __post_init__(self) -> None:
        _require_nonblank(self.code, "Discrepancy code")
        if not isinstance(self.severity, DiscrepancySeverity):
            raise PaperExecutionConfigurationError(
                "Discrepancy severity is invalid."
            )
        _require_nonblank(self.message, "Discrepancy message")
        if self.symbol is not None:
            _require_symbol(self.symbol)
        if self.client_order_id is not None:
            _require_client_order_id(self.client_order_id)

    def to_document(self) -> dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "symbol": self.symbol,
            "client_order_id": self.client_order_id,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    internal_snapshot_digest: str
    broker_snapshot_digest: str
    policy_digest: str
    compared_at: datetime
    discrepancies: tuple[ReconciliationDiscrepancy, ...]
    decision: ReconciliationDecision
    recommended_kill_switch: KillSwitchMode
    report_id: str = field(init=False)
    report_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(
            self.internal_snapshot_digest,
            "Internal snapshot digest",
        )
        _require_sha256(self.broker_snapshot_digest, "Broker snapshot digest")
        _require_sha256(self.policy_digest, "Policy digest")
        _require_aware(self.compared_at, "Reconciliation time")
        if not isinstance(self.decision, ReconciliationDecision):
            raise PaperExecutionConfigurationError(
                "Reconciliation decision is invalid."
            )
        if not isinstance(self.recommended_kill_switch, KillSwitchMode):
            raise PaperExecutionConfigurationError(
                "Recommended kill-switch mode is invalid."
            )
        if self.decision == ReconciliationDecision.PASS and self.discrepancies:
            raise PaperExecutionIntegrityError(
                "Passing reconciliation cannot contain discrepancies."
            )
        if self.decision != ReconciliationDecision.PASS and not self.discrepancies:
            raise PaperExecutionIntegrityError(
                "Failed reconciliation requires discrepancies."
            )
        identity = self._identity_document()
        digest = sha256_document(identity)
        object.__setattr__(self, "report_digest", digest)
        object.__setattr__(
            self,
            "report_id",
            str(uuid5(_REPORT_NAMESPACE, digest)),
        )

    def _identity_document(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "internal_snapshot_digest": self.internal_snapshot_digest,
            "broker_snapshot_digest": self.broker_snapshot_digest,
            "policy_digest": self.policy_digest,
            "discrepancies": [item.to_document() for item in self.discrepancies],
            "decision": self.decision.value,
            "recommended_kill_switch": self.recommended_kill_switch.value,
        }

    def to_document(self) -> dict[str, object]:
        return {
            **self._identity_document(),
            "report_id": self.report_id,
            "report_digest": self.report_digest,
            "compared_at": format_utc(self.compared_at),
        }


@dataclass(frozen=True, slots=True)
class BrokerCertificationReport:
    profile_digest: str
    policy_digest: str
    account_snapshot_digest: str
    certified: bool
    reasons: tuple[str, ...]
    evaluated_at: datetime
    report_id: str = field(init=False)
    report_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.profile_digest, "Profile digest")
        _require_sha256(self.policy_digest, "Policy digest")
        _require_sha256(
            self.account_snapshot_digest,
            "Account snapshot digest",
        )
        _require_bool(self.certified, "Certification result")
        if self.certified and self.reasons:
            raise PaperExecutionIntegrityError(
                "Certified broker reports cannot contain failure reasons."
            )
        if not self.certified and not self.reasons:
            raise PaperExecutionIntegrityError(
                "Rejected broker certification requires reasons."
            )
        for reason in self.reasons:
            _require_nonblank(reason, "Certification reason")
        _require_aware(self.evaluated_at, "Certification time")
        identity = self._identity_document()
        digest = sha256_document(identity)
        object.__setattr__(self, "report_digest", digest)
        object.__setattr__(
            self,
            "report_id",
            str(uuid5(_REPORT_NAMESPACE, digest)),
        )

    def _identity_document(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "profile_digest": self.profile_digest,
            "policy_digest": self.policy_digest,
            "account_snapshot_digest": self.account_snapshot_digest,
            "certified": self.certified,
            "reasons": list(self.reasons),
        }

    def to_document(self) -> dict[str, object]:
        return {
            **self._identity_document(),
            "report_id": self.report_id,
            "report_digest": self.report_digest,
            "evaluated_at": format_utc(self.evaluated_at),
        }


def parse_capability_profile(document: object) -> BrokerCapabilityProfile:
    mapping = _require_mapping(document, "Capability profile")
    return BrokerCapabilityProfile(
        provider=_required_str(mapping, "provider"),
        environment=_parse_enum(
            PaperBrokerEnvironment,
            mapping.get("environment"),
            "Broker environment",
        ),
        account_fingerprint=_required_str(mapping, "account_fingerprint"),
        endpoint_fingerprint=_required_str(mapping, "endpoint_fingerprint"),
        currency=_required_str(mapping, "currency"),
        read_only_access=_required_bool(mapping, "read_only_access"),
        paper_order_submission=_required_bool(
            mapping,
            "paper_order_submission",
        ),
        cancellation=_required_bool(mapping, "cancellation"),
        client_order_id_idempotency=_required_bool(
            mapping,
            "client_order_id_idempotency",
        ),
        margin=_optional_bool(mapping, "margin", False),
        short_selling=_optional_bool(mapping, "short_selling", False),
        fractional_quantity=_optional_bool(
            mapping,
            "fractional_quantity",
            False,
        ),
    )


def parse_cash_balance(document: object) -> PaperCashBalance:
    mapping = _require_mapping(document, "Cash balance")
    return PaperCashBalance(
        currency=_required_str(mapping, "currency"),
        settled_cash=decimal_from(mapping.get("settled_cash"), "Settled cash"),
        buying_power=decimal_from(mapping.get("buying_power"), "Buying power"),
    )


def parse_position(document: object) -> PaperPosition:
    mapping = _require_mapping(document, "Position")
    return PaperPosition(
        symbol=_required_str(mapping, "symbol"),
        quantity=_required_int(mapping, "quantity"),
        average_price=decimal_from(
            mapping.get("average_price"),
            "Average price",
        ),
    )


def parse_market_snapshot(document: object) -> PaperMarketSnapshot:
    mapping = _require_mapping(document, "Market snapshot")
    return PaperMarketSnapshot(
        symbol=_required_str(mapping, "symbol"),
        bid=decimal_from(mapping.get("bid"), "Bid"),
        ask=decimal_from(mapping.get("ask"), "Ask"),
        last=decimal_from(mapping.get("last"), "Last price"),
        captured_at=parse_utc_datetime(
            mapping.get("captured_at"),
            "Market snapshot time",
        ),
        source_digest=_required_str(mapping, "source_digest"),
        source=_optional_str(mapping, "source", "offline_snapshot"),
    )


def parse_order_intent(document: object) -> PaperOrderIntent:
    mapping = _require_mapping(document, "Order intent")
    intent = PaperOrderIntent(
        decision_id=_required_str(mapping, "decision_id"),
        symbol=_required_str(mapping, "symbol"),
        side=_parse_enum(
            PaperOrderSide,
            mapping.get("side"),
            "Order side",
        ),
        quantity=_required_int(mapping, "quantity"),
        limit_price=decimal_from(mapping.get("limit_price"), "Limit price"),
        created_at=parse_utc_datetime(
            mapping.get("created_at"),
            "Order intent time",
        ),
        market_snapshot_digest=_required_str(
            mapping,
            "market_snapshot_digest",
        ),
        strategy_id=_required_str(mapping, "strategy_id"),
        reason=_required_str(mapping, "reason"),
        order_type=_parse_enum(
            PaperOrderType,
            mapping.get("order_type", PaperOrderType.LIMIT.value),
            "Order type",
        ),
    )
    _verify_optional_identity(mapping, "intent_id", intent.intent_id)
    _verify_optional_identity(
        mapping,
        "client_order_id",
        intent.client_order_id,
    )
    return intent


def parse_broker_order(document: object) -> BrokerOrderSnapshot:
    mapping = _require_mapping(document, "Broker order")
    average = mapping.get("average_fill_price")
    return BrokerOrderSnapshot(
        client_order_id=_required_str(mapping, "client_order_id"),
        broker_order_id=_required_str(mapping, "broker_order_id"),
        symbol=_required_str(mapping, "symbol"),
        side=_parse_enum(
            PaperOrderSide,
            mapping.get("side"),
            "Broker order side",
        ),
        quantity=_required_int(mapping, "quantity"),
        limit_price=decimal_from(mapping.get("limit_price"), "Limit price"),
        filled_quantity=_required_int(mapping, "filled_quantity"),
        average_fill_price=(
            None
            if average is None
            else decimal_from(average, "Average fill price")
        ),
        status=_parse_enum(
            PaperOrderStatus,
            mapping.get("status"),
            "Broker order status",
        ),
        submitted_at=parse_utc_datetime(
            mapping.get("submitted_at"),
            "Broker submission time",
        ),
        updated_at=parse_utc_datetime(
            mapping.get("updated_at"),
            "Broker order update time",
        ),
    )


def parse_fill(document: object) -> PaperFillSnapshot:
    mapping = _require_mapping(document, "Fill")
    return PaperFillSnapshot(
        fill_id=_required_str(mapping, "fill_id"),
        broker_order_id=_required_str(mapping, "broker_order_id"),
        client_order_id=_required_str(mapping, "client_order_id"),
        symbol=_required_str(mapping, "symbol"),
        side=_parse_enum(
            PaperOrderSide,
            mapping.get("side"),
            "Fill side",
        ),
        quantity=_required_int(mapping, "quantity"),
        price=decimal_from(mapping.get("price"), "Fill price"),
        commission=decimal_from(mapping.get("commission"), "Fill commission"),
        filled_at=parse_utc_datetime(mapping.get("filled_at"), "Fill time"),
    )


def parse_account_snapshot(document: object) -> PaperAccountSnapshot:
    mapping = _require_mapping(document, "Account snapshot")
    snapshot = PaperAccountSnapshot(
        profile=parse_capability_profile(mapping.get("profile")),
        captured_at=parse_utc_datetime(
            mapping.get("captured_at"),
            "Account snapshot time",
        ),
        cash=parse_cash_balance(mapping.get("cash")),
        positions=tuple(
            parse_position(item)
            for item in _required_list(mapping, "positions")
        ),
        orders=tuple(
            parse_broker_order(item)
            for item in _required_list(mapping, "orders")
        ),
        fills=tuple(
            parse_fill(item) for item in _required_list(mapping, "fills")
        ),
        source_digest=_required_str(mapping, "source_digest"),
    )
    _verify_optional_identity(
        mapping,
        "snapshot_digest",
        snapshot.snapshot_digest,
    )
    return snapshot


def parse_policy(document: object) -> PaperExecutionPolicy:
    mapping = _require_mapping(document, "Paper execution policy")
    symbols = _required_list(mapping, "allowed_symbols")
    return PaperExecutionPolicy(
        expected_provider=_required_str(mapping, "expected_provider"),
        expected_environment=_parse_enum(
            PaperBrokerEnvironment,
            mapping.get("expected_environment"),
            "Expected environment",
        ),
        expected_account_fingerprint=_required_str(
            mapping,
            "expected_account_fingerprint",
        ),
        expected_endpoint_fingerprint=_required_str(
            mapping,
            "expected_endpoint_fingerprint",
        ),
        currency=_required_str(mapping, "currency"),
        allowed_symbols=tuple(
            _list_str(item, "Allowed symbol") for item in symbols
        ),
        maximum_market_age_seconds=_required_int(
            mapping,
            "maximum_market_age_seconds",
        ),
        maximum_account_age_seconds=_required_int(
            mapping,
            "maximum_account_age_seconds",
        ),
        maximum_order_quantity=_required_int(
            mapping,
            "maximum_order_quantity",
        ),
        maximum_order_notional=decimal_from(
            mapping.get("maximum_order_notional"),
            "Maximum order notional",
        ),
        maximum_position_quantity=_required_int(
            mapping,
            "maximum_position_quantity",
        ),
        maximum_open_orders=_required_int(mapping, "maximum_open_orders"),
        minimum_cash_reserve_fraction=decimal_from(
            mapping.get("minimum_cash_reserve_fraction"),
            "Minimum cash reserve fraction",
        ),
        maximum_limit_deviation_bps=decimal_from(
            mapping.get("maximum_limit_deviation_bps"),
            "Maximum limit deviation bps",
        ),
        cash_reconciliation_tolerance=decimal_from(
            mapping.get("cash_reconciliation_tolerance"),
            "Cash reconciliation tolerance",
        ),
        require_marketable_limit=_optional_bool(
            mapping,
            "require_marketable_limit",
            True,
        ),
    )


def parse_internal_order_state(document: object) -> InternalOrderState:
    mapping = _require_mapping(document, "Internal order state")
    broker_order_id = mapping.get("broker_order_id")
    if broker_order_id is not None and not isinstance(broker_order_id, str):
        raise PaperExecutionConfigurationError(
            "Broker order ID must be text or null."
        )
    return InternalOrderState(
        client_order_id=_required_str(mapping, "client_order_id"),
        broker_order_id=broker_order_id,
        status=_parse_enum(
            PaperOrderStatus,
            mapping.get("status"),
            "Internal order status",
        ),
        filled_quantity=_required_int(mapping, "filled_quantity"),
    )


def parse_internal_ledger(document: object) -> InternalPaperLedgerSnapshot:
    mapping = _require_mapping(document, "Internal ledger snapshot")
    snapshot = InternalPaperLedgerSnapshot(
        account_fingerprint=_required_str(mapping, "account_fingerprint"),
        captured_at=parse_utc_datetime(
            mapping.get("captured_at"),
            "Internal ledger time",
        ),
        cash=parse_cash_balance(mapping.get("cash")),
        positions=tuple(
            parse_position(item)
            for item in _required_list(mapping, "positions")
        ),
        orders=tuple(
            parse_internal_order_state(item)
            for item in _required_list(mapping, "orders")
        ),
        fill_ids=tuple(
            _list_str(item, "Fill ID")
            for item in _required_list(mapping, "fill_ids")
        ),
    )
    _verify_optional_identity(
        mapping,
        "snapshot_digest",
        snapshot.snapshot_digest,
    )
    return snapshot


def _validate_status_quantities(
    status: PaperOrderStatus,
    quantity: int,
    filled_quantity: int,
) -> None:
    if status == PaperOrderStatus.FILLED and filled_quantity != quantity:
        raise PaperExecutionIntegrityError(
            "Filled status requires the full quantity to be filled."
        )
    if status == PaperOrderStatus.PARTIALLY_FILLED and not (
        0 < filled_quantity < quantity
    ):
        raise PaperExecutionIntegrityError(
            "Partially-filled status requires a partial positive fill."
        )
    if status in {
        PaperOrderStatus.CREATED,
        PaperOrderStatus.VALIDATED,
        PaperOrderStatus.SUBMITTING,
        PaperOrderStatus.SUBMITTED,
        PaperOrderStatus.REJECTED,
    } and filled_quantity != 0:
        raise PaperExecutionIntegrityError(
            f"{status.value} status cannot have a filled quantity."
        )


def _require_sorted_unique_positions(
    positions: tuple[PaperPosition, ...],
) -> None:
    symbols = tuple(item.symbol for item in positions)
    if symbols != tuple(sorted(symbols)):
        raise PaperExecutionConfigurationError(
            "Positions must be sorted by symbol."
        )
    if len(set(symbols)) != len(symbols):
        raise PaperExecutionIntegrityError("Positions cannot repeat a symbol.")


def _require_sorted_unique_orders(
    orders: tuple[BrokerOrderSnapshot, ...],
) -> None:
    client_ids = tuple(item.client_order_id for item in orders)
    if client_ids != tuple(sorted(client_ids)):
        raise PaperExecutionConfigurationError(
            "Broker orders must be sorted by client order ID."
        )
    if len(set(client_ids)) != len(client_ids):
        raise PaperExecutionIntegrityError(
            "Broker orders cannot repeat a client order ID."
        )
    broker_ids = tuple(item.broker_order_id for item in orders)
    if len(set(broker_ids)) != len(broker_ids):
        raise PaperExecutionIntegrityError(
            "Broker orders cannot repeat a broker order ID."
        )


def _require_sorted_unique_fills(fills: tuple[PaperFillSnapshot, ...]) -> None:
    fill_ids = tuple(item.fill_id for item in fills)
    if fill_ids != tuple(sorted(fill_ids)):
        raise PaperExecutionConfigurationError("Fills must be sorted by fill ID.")
    if len(set(fill_ids)) != len(fill_ids):
        raise PaperExecutionIntegrityError("Fills cannot repeat a fill ID.")


def _require_mapping(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PaperExecutionConfigurationError(f"{field_name} must be an object.")
    if not all(isinstance(key, str) for key in value):
        raise PaperExecutionConfigurationError(
            f"{field_name} keys must be text."
        )
    return value


def _required_list(mapping: dict[str, object], key: str) -> list[object]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise PaperExecutionConfigurationError(f"{key} must be a list.")
    return value


def _required_str(mapping: dict[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PaperExecutionConfigurationError(f"{key} must be nonblank text.")
    return value


def _optional_str(
    mapping: dict[str, object],
    key: str,
    default: str,
) -> str:
    value = mapping.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise PaperExecutionConfigurationError(f"{key} must be nonblank text.")
    return value


def _required_bool(mapping: dict[str, object], key: str) -> bool:
    value = mapping.get(key)
    if not isinstance(value, bool):
        raise PaperExecutionConfigurationError(f"{key} must be a boolean.")
    return value


def _optional_bool(
    mapping: dict[str, object],
    key: str,
    default: bool,
) -> bool:
    value = mapping.get(key, default)
    if not isinstance(value, bool):
        raise PaperExecutionConfigurationError(f"{key} must be a boolean.")
    return value


def _required_int(mapping: dict[str, object], key: str) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PaperExecutionConfigurationError(f"{key} must be an integer.")
    return value


def _list_str(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PaperExecutionConfigurationError(
            f"{field_name} must be nonblank text."
        )
    return value


def _parse_enum[EnumT: StrEnum](
    enum_type: type[EnumT],
    value: object,
    field_name: str,
) -> EnumT:
    if not isinstance(value, str):
        raise PaperExecutionConfigurationError(f"{field_name} must be text.")
    try:
        return enum_type(value)
    except ValueError as error:
        raise PaperExecutionConfigurationError(
            f"{field_name} has an unsupported value: {value}."
        ) from error


def _verify_optional_identity(
    mapping: dict[str, object],
    key: str,
    expected: str,
) -> None:
    value = mapping.get(key)
    if value is not None and value != expected:
        raise PaperExecutionIntegrityError(
            f"{key} does not match the deterministic identity."
        )


def _require_bool(value: object, field_name: str) -> None:
    if not isinstance(value, bool):
        raise PaperExecutionConfigurationError(f"{field_name} must be boolean.")


def _require_nonblank(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise PaperExecutionConfigurationError(
            f"{field_name} cannot be blank."
        )


def _require_symbol(value: str) -> None:
    _require_nonblank(value, "Symbol")
    if value != value.strip().upper():
        raise PaperExecutionConfigurationError(
            "Symbols must be uppercase without surrounding whitespace."
        )
    if len(value) > 32:
        raise PaperExecutionConfigurationError("Symbols cannot exceed 32 characters.")


def _require_client_order_id(value: str) -> None:
    _require_nonblank(value, "Client order ID")
    if not value.startswith("wqs-") or len(value) != 32:
        raise PaperExecutionConfigurationError(
            "Client order IDs must use the deterministic wqs- format."
        )


def _require_currency(value: str) -> None:
    _require_nonblank(value, "Currency")
    if len(value) != 3 or value != value.upper():
        raise PaperExecutionConfigurationError(
            "Currency must be an uppercase three-letter code."
        )


def _require_sha256(value: str, field_name: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise PaperExecutionConfigurationError(
            f"{field_name} must be a SHA-256 hex digest."
        )
    try:
        int(value, 16)
    except ValueError as error:
        raise PaperExecutionConfigurationError(
            f"{field_name} must be a SHA-256 hex digest."
        ) from error


def _require_aware(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PaperExecutionConfigurationError(
            f"{field_name} must include timezone information."
        )
    if value.utcoffset() is None:
        raise PaperExecutionConfigurationError(
            f"{field_name} must include timezone information."
        )


def _require_finite_decimal(value: Decimal, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise PaperExecutionConfigurationError(
            f"{field_name} must be a finite Decimal."
        )


def _require_nonnegative_decimal(value: Decimal, field_name: str) -> None:
    _require_finite_decimal(value, field_name)
    if value < _ZERO:
        raise PaperExecutionConfigurationError(
            f"{field_name} cannot be negative."
        )


def _require_positive_decimal(value: Decimal, field_name: str) -> None:
    _require_finite_decimal(value, field_name)
    if value <= _ZERO:
        raise PaperExecutionConfigurationError(
            f"{field_name} must be positive."
        )


def _require_unit_interval(
    value: Decimal,
    field_name: str,
    *,
    include_one: bool,
) -> None:
    _require_finite_decimal(value, field_name)
    upper_valid = value <= _ONE if include_one else value < _ONE
    if value < _ZERO or not upper_valid:
        right = "1" if include_one else "1 (exclusive)"
        raise PaperExecutionConfigurationError(
            f"{field_name} must be between 0 and {right}."
        )


def _require_nonnegative_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PaperExecutionConfigurationError(
            f"{field_name} must be a nonnegative integer."
        )


def _require_positive_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PaperExecutionConfigurationError(
            f"{field_name} must be a positive integer."
        )


def _decimal_text(value: Decimal) -> str:
    if value == _ZERO:
        return "0"
    return format(value.normalize(), "f")


