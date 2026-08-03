from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid5

from world_quant_system.broker_certification.models import CertificationStatus
from world_quant_system.paper_execution.models import (
    KillSwitchState,
    PaperOrderIntent,
)

OFFICIAL_TOSS_BASE_URL: Final[str] = "https://openapi.tossinvest.com"
OFFICIAL_ORDER_CREATE_PATH: Final[str] = "/api/v1/orders"
OFFICIAL_ORDER_CREATE_METHOD: Final[str] = "POST"
OFFICIAL_ORDER_OPERATION_ID: Final[str] = "createOrder"
PINNED_OPENAPI_VERSION: Final[str] = "1.2.9"
OFFICIAL_CLIENT_ORDER_ID_TTL_SECONDS: Final[int] = 600
OFFICIAL_HIGH_VALUE_THRESHOLD_KRW: Final[Decimal] = Decimal("100000000")

_SCHEMA_VERSION: Final[int] = 1
_REPORT_NAMESPACE: Final[UUID] = UUID("e54082dd-827a-54bb-bb3d-719c971b19af")
_APPROVAL_NAMESPACE: Final[UUID] = UUID("2c73a4c6-d886-55e0-b387-8083c8fe894c")
_CLIENT_ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,36}$")
_KR_SYMBOL_PATTERN = re.compile(r"^[0-9]{6}$")
_US_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.\-]{0,15}$")
_ZERO = Decimal("0")
_BPS = Decimal("10000")


class OrderWriteCertificationError(Exception):
    """Base exception for order-write dry-run certification failures."""


class OrderWriteConfigurationError(OrderWriteCertificationError):
    """Raised when configuration or input is invalid."""


class OrderWriteIntegrityError(OrderWriteCertificationError):
    """Raised when deterministic evidence is inconsistent."""


class OrderWriteSafetyError(OrderWriteCertificationError):
    """Raised when a write-safety invariant is violated."""


class DryRunDecision(StrEnum):
    HUMAN_APPROVAL_REQUIRED = "human_approval_required"
    REJECTED = "rejected"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class DryRunTransportOutcome(StrEnum):
    NOT_SUBMITTED = "not_submitted"


class TossOrderMarket(StrEnum):
    KR = "KR"
    US = "US"


class TossTimeInForce(StrEnum):
    DAY = "DAY"


@dataclass(frozen=True, slots=True)
class TossOrderCreateContract:
    base_url: str = OFFICIAL_TOSS_BASE_URL
    path: str = OFFICIAL_ORDER_CREATE_PATH
    method: str = OFFICIAL_ORDER_CREATE_METHOD
    operation_id: str = OFFICIAL_ORDER_OPERATION_ID
    openapi_version: str = PINNED_OPENAPI_VERSION
    account_header_required: bool = True
    oauth_bearer_required: bool = True
    client_order_id_max_length: int = 36
    client_order_id_ttl_seconds: int = OFFICIAL_CLIENT_ORDER_ID_TTL_SECONDS
    quantity_based_limit_only: bool = True
    network_transport_enabled: bool = False

    def __post_init__(self) -> None:
        if self.base_url != OFFICIAL_TOSS_BASE_URL:
            raise OrderWriteSafetyError("Unexpected Toss API base URL.")
        if self.path != OFFICIAL_ORDER_CREATE_PATH:
            raise OrderWriteSafetyError("Unexpected Toss order-create path.")
        if self.method != OFFICIAL_ORDER_CREATE_METHOD:
            raise OrderWriteSafetyError("Unexpected Toss order-create method.")
        if self.operation_id != OFFICIAL_ORDER_OPERATION_ID:
            raise OrderWriteSafetyError("Unexpected Toss order operation ID.")
        if self.openapi_version != PINNED_OPENAPI_VERSION:
            raise OrderWriteSafetyError("Unexpected pinned OpenAPI version.")
        if not self.account_header_required or not self.oauth_bearer_required:
            raise OrderWriteSafetyError(
                "Toss order creation requires account and bearer headers."
            )
        if self.client_order_id_max_length != 36:
            raise OrderWriteSafetyError("Unexpected client-order-ID limit.")
        if self.client_order_id_ttl_seconds != 600:
            raise OrderWriteSafetyError("Unexpected idempotency TTL.")
        if not self.quantity_based_limit_only:
            raise OrderWriteSafetyError(
                "Dry-run v1 must remain quantity-based and limit-only."
            )
        if self.network_transport_enabled:
            raise OrderWriteSafetyError(
                "Dry-run certification cannot enable network transport."
            )

    @property
    def contract_digest(self) -> str:
        return sha256_document(self.to_document())

    def to_document(self) -> dict[str, object]:
        return {
            "base_url": self.base_url,
            "path": self.path,
            "method": self.method,
            "operation_id": self.operation_id,
            "openapi_version": self.openapi_version,
            "account_header_required": self.account_header_required,
            "oauth_bearer_required": self.oauth_bearer_required,
            "client_order_id_max_length": self.client_order_id_max_length,
            "client_order_id_ttl_seconds": self.client_order_id_ttl_seconds,
            "quantity_based_limit_only": self.quantity_based_limit_only,
            "network_transport_enabled": self.network_transport_enabled,
        }


@dataclass(frozen=True, slots=True)
class ReadOnlyCertificationEvidence:
    provider: str
    base_url: str
    certified_at: datetime
    account_fingerprint: str
    status: CertificationStatus
    report_digest: str
    network_transport_enabled: bool
    write_operations_enabled: bool

    def __post_init__(self) -> None:
        _require_nonblank(self.provider, "Provider")
        if self.base_url != OFFICIAL_TOSS_BASE_URL:
            raise OrderWriteSafetyError(
                "Read-only evidence uses an unexpected base URL."
            )
        _require_aware(self.certified_at, "Certification time")
        _require_sha256(self.account_fingerprint, "Account fingerprint")
        if not isinstance(self.status, CertificationStatus):
            raise OrderWriteConfigurationError("Certification status is invalid.")
        _require_sha256(self.report_digest, "Certification report digest")
        _require_bool(
            self.network_transport_enabled,
            "Network-transport enabled flag",
        )
        _require_bool(
            self.write_operations_enabled,
            "Write-operations enabled flag",
        )
        if self.write_operations_enabled:
            raise OrderWriteSafetyError(
                "Read-only certification evidence cannot enable writes."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "certified_at": format_utc(self.certified_at),
            "account_fingerprint": self.account_fingerprint,
            "status": self.status.value,
            "report_digest": self.report_digest,
            "network_transport_enabled": self.network_transport_enabled,
            "write_operations_enabled": self.write_operations_enabled,
        }


@dataclass(frozen=True, slots=True)
class DryRunPosition:
    symbol: str
    quantity: int

    def __post_init__(self) -> None:
        _require_symbol(self.symbol)
        _require_nonnegative_int(self.quantity, "Position quantity")

    def to_document(self) -> dict[str, object]:
        return {"symbol": self.symbol, "quantity": self.quantity}


@dataclass(frozen=True, slots=True)
class DryRunAccountState:
    account_fingerprint: str
    currency: str
    settled_cash: Decimal
    buying_power: Decimal
    positions: tuple[DryRunPosition, ...]
    open_order_count: int
    captured_at: datetime
    source_digest: str
    snapshot_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.account_fingerprint, "Account fingerprint")
        _require_currency(self.currency)
        _require_nonnegative_decimal(self.settled_cash, "Settled cash")
        _require_nonnegative_decimal(self.buying_power, "Buying power")
        _require_nonnegative_int(self.open_order_count, "Open-order count")
        _require_aware(self.captured_at, "Account state time")
        _require_sha256(self.source_digest, "Account source digest")
        symbols = tuple(position.symbol for position in self.positions)
        if symbols != tuple(sorted(symbols)):
            raise OrderWriteConfigurationError(
                "Account positions must be sorted by symbol."
            )
        if len(set(symbols)) != len(symbols):
            raise OrderWriteIntegrityError(
                "Account positions cannot contain duplicate symbols."
            )
        object.__setattr__(
            self,
            "snapshot_digest",
            sha256_document(self._identity_document()),
        )

    @property
    def positions_by_symbol(self) -> dict[str, DryRunPosition]:
        return {position.symbol: position for position in self.positions}

    def _identity_document(self) -> dict[str, object]:
        return {
            "account_fingerprint": self.account_fingerprint,
            "currency": self.currency,
            "settled_cash": decimal_text(self.settled_cash),
            "buying_power": decimal_text(self.buying_power),
            "positions": [position.to_document() for position in self.positions],
            "open_order_count": self.open_order_count,
            "captured_at": format_utc(self.captured_at),
            "source_digest": self.source_digest,
        }

    def to_document(self) -> dict[str, object]:
        return {
            **self._identity_document(),
            "snapshot_digest": self.snapshot_digest,
        }


@dataclass(frozen=True, slots=True)
class DryRunMarketState:
    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    currency: str
    captured_at: datetime
    source_digest: str
    lower_limit_price: Decimal | None = None
    upper_limit_price: Decimal | None = None
    snapshot_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _require_symbol(self.symbol)
        for price_value, field_name in (
            (self.bid, "Bid"),
            (self.ask, "Ask"),
            (self.last, "Last"),
        ):
            _require_positive_decimal(price_value, field_name)
        if self.ask < self.bid:
            raise OrderWriteIntegrityError("Ask cannot be below bid.")
        _require_currency(self.currency)
        _require_aware(self.captured_at, "Market state time")
        _require_sha256(self.source_digest, "Market source digest")
        if self.lower_limit_price is not None:
            _require_positive_decimal(
                self.lower_limit_price,
                "Lower limit price",
            )
        if self.upper_limit_price is not None:
            _require_positive_decimal(
                self.upper_limit_price,
                "Upper limit price",
            )
        if (
            self.lower_limit_price is not None
            and self.upper_limit_price is not None
            and self.upper_limit_price < self.lower_limit_price
        ):
            raise OrderWriteIntegrityError(
                "Upper price limit cannot be below lower price limit."
            )
        object.__setattr__(
            self,
            "snapshot_digest",
            sha256_document(self._identity_document()),
        )

    def _identity_document(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "bid": decimal_text(self.bid),
            "ask": decimal_text(self.ask),
            "last": decimal_text(self.last),
            "currency": self.currency,
            "captured_at": format_utc(self.captured_at),
            "source_digest": self.source_digest,
            "lower_limit_price": (
                None
                if self.lower_limit_price is None
                else decimal_text(self.lower_limit_price)
            ),
            "upper_limit_price": (
                None
                if self.upper_limit_price is None
                else decimal_text(self.upper_limit_price)
            ),
        }

    def to_document(self) -> dict[str, object]:
        return {
            **self._identity_document(),
            "snapshot_digest": self.snapshot_digest,
        }


@dataclass(frozen=True, slots=True)
class TossOrderWritePolicy:
    expected_account_fingerprint: str
    allowed_symbols: tuple[str, ...]
    currency: str
    market: TossOrderMarket
    maximum_market_age_seconds: int
    maximum_account_age_seconds: int
    maximum_certification_age_seconds: int
    maximum_order_quantity: int
    maximum_order_notional: Decimal
    maximum_position_quantity: int
    maximum_open_orders: int
    minimum_cash_reserve_fraction: Decimal
    maximum_limit_deviation_bps: Decimal
    price_tick: Decimal
    human_approval_ttl_seconds: int
    high_value_order_threshold: Decimal
    require_marketable_limit: bool = True

    def __post_init__(self) -> None:
        _require_sha256(
            self.expected_account_fingerprint,
            "Expected account fingerprint",
        )
        if not self.allowed_symbols:
            raise OrderWriteConfigurationError(
                "At least one symbol must be allowed."
            )
        if self.allowed_symbols != tuple(sorted(self.allowed_symbols)):
            raise OrderWriteConfigurationError("Allowed symbols must be sorted.")
        if len(set(self.allowed_symbols)) != len(self.allowed_symbols):
            raise OrderWriteConfigurationError(
                "Allowed symbols cannot contain duplicates."
            )
        for symbol in self.allowed_symbols:
            _require_symbol_for_market(symbol, self.market)
        _require_currency(self.currency)
        if not isinstance(self.market, TossOrderMarket):
            raise OrderWriteConfigurationError("Order market is invalid.")
        if self.market is TossOrderMarket.KR and self.currency != "KRW":
            raise OrderWriteConfigurationError("KR market requires KRW.")
        if self.market is TossOrderMarket.US and self.currency != "USD":
            raise OrderWriteConfigurationError("US market requires USD.")
        for integer_value, field_name in (
            (self.maximum_market_age_seconds, "Maximum market age"),
            (self.maximum_account_age_seconds, "Maximum account age"),
            (
                self.maximum_certification_age_seconds,
                "Maximum certification age",
            ),
            (self.maximum_order_quantity, "Maximum order quantity"),
            (self.maximum_position_quantity, "Maximum position quantity"),
            (self.maximum_open_orders, "Maximum open orders"),
            (self.human_approval_ttl_seconds, "Human approval TTL"),
        ):
            _require_positive_int(integer_value, field_name)
        for decimal_value, field_name in (
            (self.maximum_order_notional, "Maximum order notional"),
            (self.price_tick, "Price tick"),
            (self.high_value_order_threshold, "High-value threshold"),
        ):
            _require_positive_decimal(decimal_value, field_name)
        _require_unit_interval(
            self.minimum_cash_reserve_fraction,
            "Minimum cash reserve fraction",
        )
        _require_nonnegative_decimal(
            self.maximum_limit_deviation_bps,
            "Maximum limit deviation bps",
        )
        _require_bool(
            self.require_marketable_limit,
            "Marketable-limit requirement",
        )
        if self.maximum_order_notional >= self.high_value_order_threshold:
            raise OrderWriteSafetyError(
                "Maximum notional must stay below the high-value threshold."
            )
        if (
            self.market is TossOrderMarket.KR
            and self.high_value_order_threshold
            != OFFICIAL_HIGH_VALUE_THRESHOLD_KRW
        ):
            raise OrderWriteSafetyError(
                "KR high-value threshold must match the pinned contract."
            )

    @property
    def policy_digest(self) -> str:
        return sha256_document(self.to_document())

    def to_document(self) -> dict[str, object]:
        return {
            "expected_account_fingerprint": self.expected_account_fingerprint,
            "allowed_symbols": list(self.allowed_symbols),
            "currency": self.currency,
            "market": self.market.value,
            "maximum_market_age_seconds": self.maximum_market_age_seconds,
            "maximum_account_age_seconds": self.maximum_account_age_seconds,
            "maximum_certification_age_seconds": (
                self.maximum_certification_age_seconds
            ),
            "maximum_order_quantity": self.maximum_order_quantity,
            "maximum_order_notional": decimal_text(
                self.maximum_order_notional
            ),
            "maximum_position_quantity": self.maximum_position_quantity,
            "maximum_open_orders": self.maximum_open_orders,
            "minimum_cash_reserve_fraction": decimal_text(
                self.minimum_cash_reserve_fraction
            ),
            "maximum_limit_deviation_bps": decimal_text(
                self.maximum_limit_deviation_bps
            ),
            "price_tick": decimal_text(self.price_tick),
            "human_approval_ttl_seconds": self.human_approval_ttl_seconds,
            "high_value_order_threshold": decimal_text(
                self.high_value_order_threshold
            ),
            "require_marketable_limit": self.require_marketable_limit,
        }


@dataclass(frozen=True, slots=True)
class DryRunCheck:
    code: str
    passed: bool
    message: str

    def __post_init__(self) -> None:
        _require_nonblank(self.code, "Check code")
        _require_bool(self.passed, "Check result")
        _require_nonblank(self.message, "Check message")

    def to_document(self) -> dict[str, object]:
        return {
            "code": self.code,
            "passed": self.passed,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class CompiledTossOrderRequest:
    contract: TossOrderCreateContract
    account_fingerprint: str
    headers: tuple[tuple[str, str], ...]
    body: tuple[tuple[str, object], ...]
    compiled_at: datetime
    network_sendable: bool = False
    credentials_loaded: bool = False
    request_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.contract, TossOrderCreateContract):
            raise OrderWriteConfigurationError("Write contract is invalid.")
        _require_sha256(self.account_fingerprint, "Account fingerprint")
        _require_aware(self.compiled_at, "Request compile time")
        if self.network_sendable:
            raise OrderWriteSafetyError(
                "Dry-run requests cannot be network-sendable."
            )
        if self.credentials_loaded:
            raise OrderWriteSafetyError(
                "Dry-run requests cannot load credentials."
            )
        if self.headers != tuple(sorted(self.headers)):
            raise OrderWriteConfigurationError("Headers must be sorted.")
        normalized_headers = {name.lower(): value for name, value in self.headers}
        if normalized_headers.get("authorization") != "Bearer [REDACTED]":
            raise OrderWriteSafetyError(
                "Authorization must remain a redacted placeholder."
            )
        expected_account_header = (
            f"sha256:{self.account_fingerprint}"
        )
        if (
            normalized_headers.get("x-tossinvest-account")
            != expected_account_header
        ):
            raise OrderWriteSafetyError(
                "Account header must contain only the account fingerprint."
            )
        if normalized_headers.get("content-type") != "application/json":
            raise OrderWriteConfigurationError(
                "Order request must use application/json."
            )
        body_document = dict(self.body)
        required_keys = {
            "clientOrderId",
            "symbol",
            "side",
            "orderType",
            "timeInForce",
            "quantity",
            "price",
            "confirmHighValueOrder",
        }
        if set(body_document) != required_keys:
            raise OrderWriteIntegrityError(
                "Compiled order body has an unexpected field set."
            )
        client_order_id = body_document["clientOrderId"]
        if not isinstance(client_order_id, str):
            raise OrderWriteConfigurationError(
                "clientOrderId must be text."
            )
        _require_client_order_id(client_order_id)
        if body_document["orderType"] != "LIMIT":
            raise OrderWriteSafetyError("Dry-run v1 permits LIMIT only.")
        if body_document["timeInForce"] != "DAY":
            raise OrderWriteSafetyError("Dry-run v1 permits DAY only.")
        if body_document["confirmHighValueOrder"] is not False:
            raise OrderWriteSafetyError(
                "Dry-run v1 cannot confirm high-value orders."
            )
        object.__setattr__(
            self,
            "request_digest",
            sha256_document(self._identity_document()),
        )

    def _identity_document(self) -> dict[str, object]:
        return {
            "contract": self.contract.to_document(),
            "account_fingerprint": self.account_fingerprint,
            "headers": [list(item) for item in self.headers],
            "body": dict(self.body),
            "compiled_at": format_utc(self.compiled_at),
            "network_sendable": self.network_sendable,
            "credentials_loaded": self.credentials_loaded,
        }

    def to_document(self) -> dict[str, object]:
        return {
            **self._identity_document(),
            "request_digest": self.request_digest,
        }


@dataclass(frozen=True, slots=True)
class HumanApprovalChallenge:
    request_digest: str
    account_fingerprint: str
    created_at: datetime
    expires_at: datetime
    approval_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.request_digest, "Request digest")
        _require_sha256(self.account_fingerprint, "Account fingerprint")
        _require_aware(self.created_at, "Approval challenge creation time")
        _require_aware(self.expires_at, "Approval challenge expiry")
        if self.expires_at <= self.created_at:
            raise OrderWriteConfigurationError(
                "Approval challenge must expire after creation."
            )
        identity = {
            "request_digest": self.request_digest,
            "account_fingerprint": self.account_fingerprint,
            "created_at": format_utc(self.created_at),
            "expires_at": format_utc(self.expires_at),
        }
        object.__setattr__(
            self,
            "approval_id",
            str(uuid5(_APPROVAL_NAMESPACE, sha256_document(identity))),
        )

    def to_document(self) -> dict[str, object]:
        return {
            "approval_id": self.approval_id,
            "request_digest": self.request_digest,
            "account_fingerprint": self.account_fingerprint,
            "created_at": format_utc(self.created_at),
            "expires_at": format_utc(self.expires_at),
            "approval_state": "required_not_granted",
        }


@dataclass(frozen=True, slots=True)
class DryRunTransportReceipt:
    request_digest: str
    evaluated_at: datetime
    outcome: DryRunTransportOutcome = DryRunTransportOutcome.NOT_SUBMITTED
    broker_write_count: int = 0
    network_call_count: int = 0

    def __post_init__(self) -> None:
        _require_sha256(self.request_digest, "Request digest")
        _require_aware(self.evaluated_at, "Dry-run transport time")
        if self.outcome is not DryRunTransportOutcome.NOT_SUBMITTED:
            raise OrderWriteSafetyError(
                "Dry-run transport outcome must remain NOT_SUBMITTED."
            )
        if self.broker_write_count != 0 or self.network_call_count != 0:
            raise OrderWriteSafetyError(
                "Dry-run transport cannot perform broker or network writes."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "request_digest": self.request_digest,
            "evaluated_at": format_utc(self.evaluated_at),
            "outcome": self.outcome.value,
            "broker_write_count": self.broker_write_count,
            "network_call_count": self.network_call_count,
        }


@dataclass(frozen=True, slots=True)
class OrderWriteDryRunReport:
    evaluated_at: datetime
    intent: PaperOrderIntent
    certification: ReadOnlyCertificationEvidence
    account: DryRunAccountState
    market: DryRunMarketState
    policy: TossOrderWritePolicy
    contract: TossOrderCreateContract
    kill_switch: KillSwitchState
    checks: tuple[DryRunCheck, ...]
    decision: DryRunDecision
    compiled_request: CompiledTossOrderRequest | None
    approval_challenge: HumanApprovalChallenge | None
    transport_receipt: DryRunTransportReceipt | None
    schema_version: int = _SCHEMA_VERSION
    report_id: str = field(init=False)
    report_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _require_aware(self.evaluated_at, "Report evaluation time")
        if not isinstance(self.intent, PaperOrderIntent):
            raise OrderWriteConfigurationError("Report intent is invalid.")
        if not isinstance(self.certification, ReadOnlyCertificationEvidence):
            raise OrderWriteConfigurationError(
                "Report certification evidence is invalid."
            )
        if not isinstance(self.account, DryRunAccountState):
            raise OrderWriteConfigurationError("Report account state is invalid.")
        if not isinstance(self.market, DryRunMarketState):
            raise OrderWriteConfigurationError("Report market state is invalid.")
        if not isinstance(self.policy, TossOrderWritePolicy):
            raise OrderWriteConfigurationError("Report policy is invalid.")
        if not isinstance(self.contract, TossOrderCreateContract):
            raise OrderWriteConfigurationError("Report contract is invalid.")
        if not isinstance(self.kill_switch, KillSwitchState):
            raise OrderWriteConfigurationError("Report kill switch is invalid.")
        if not self.checks:
            raise OrderWriteIntegrityError("Dry-run report requires checks.")
        if not isinstance(self.decision, DryRunDecision):
            raise OrderWriteConfigurationError("Report decision is invalid.")
        failed_checks = tuple(check for check in self.checks if not check.passed)
        if self.decision is DryRunDecision.HUMAN_APPROVAL_REQUIRED:
            if failed_checks:
                raise OrderWriteIntegrityError(
                    "Approval-required reports cannot have failed checks."
                )
            if (
                self.compiled_request is None
                or self.approval_challenge is None
                or self.transport_receipt is None
            ):
                raise OrderWriteIntegrityError(
                    "Successful dry runs require request, approval, and receipt."
                )
        else:
            if not failed_checks:
                raise OrderWriteIntegrityError(
                    "Rejected or insufficient reports require failed checks."
                )
            if (
                self.compiled_request is not None
                or self.approval_challenge is not None
                or self.transport_receipt is not None
            ):
                raise OrderWriteIntegrityError(
                    "Failed dry runs cannot contain compiled write artifacts."
                )
        if self.transport_receipt is not None:
            if self.transport_receipt.broker_write_count != 0:
                raise OrderWriteSafetyError("Broker write count must remain zero.")
            if self.transport_receipt.network_call_count != 0:
                raise OrderWriteSafetyError("Network call count must remain zero.")
        _require_positive_int(self.schema_version, "Schema version")
        digest = sha256_document(self._identity_document())
        object.__setattr__(self, "report_digest", digest)
        object.__setattr__(
            self,
            "report_id",
            str(uuid5(_REPORT_NAMESPACE, digest)),
        )

    def _identity_document(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "evaluated_at": format_utc(self.evaluated_at),
            "intent": self.intent.to_document(),
            "certification": self.certification.to_document(),
            "account": self.account.to_document(),
            "market": self.market.to_document(),
            "policy": self.policy.to_document(),
            "contract": self.contract.to_document(),
            "kill_switch": self.kill_switch.to_document(),
            "checks": [check.to_document() for check in self.checks],
            "decision": self.decision.value,
            "compiled_request": (
                None
                if self.compiled_request is None
                else self.compiled_request.to_document()
            ),
            "approval_challenge": (
                None
                if self.approval_challenge is None
                else self.approval_challenge.to_document()
            ),
            "transport_receipt": (
                None
                if self.transport_receipt is None
                else self.transport_receipt.to_document()
            ),
            "external_network_transport_enabled": False,
            "credentials_loaded": False,
            "broker_write_count": 0,
            "live_trading_enabled": False,
        }

    def to_document(self) -> dict[str, object]:
        return {
            **self._identity_document(),
            "report_id": self.report_id,
            "report_digest": self.report_digest,
        }


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def sha256_document(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def format_utc(value: datetime) -> str:
    _require_aware(value, "Timestamp")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_utc_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise OrderWriteConfigurationError(
            f"{field_name} must be an ISO-8601 timestamp."
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise OrderWriteConfigurationError(
            f"{field_name} must be an ISO-8601 timestamp."
        ) from error
    _require_aware(parsed, field_name)
    return parsed.astimezone(UTC)


def decimal_from(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise OrderWriteConfigurationError(
            f"{field_name} must be a decimal value."
        )
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise OrderWriteConfigurationError(
            f"{field_name} must be a decimal value."
        ) from error
    _require_finite_decimal(parsed, field_name)
    return parsed


def decimal_text(value: Decimal) -> str:
    _require_finite_decimal(value, "Decimal")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def validate_price_precision(
    *,
    price: Decimal,
    market: TossOrderMarket,
) -> bool:
    if market is TossOrderMarket.KR:
        return price == price.to_integral_value()
    exponent = price.as_tuple().exponent
    if not isinstance(exponent, int):
        raise OrderWriteConfigurationError(
            "Price must be a finite decimal value."
        )
    scale = max(0, -exponent)
    return scale <= (4 if price < Decimal("1") else 2)


def price_deviation_bps(price: Decimal, reference: Decimal) -> Decimal:
    _require_positive_decimal(price, "Price")
    _require_positive_decimal(reference, "Reference price")
    return abs(price - reference) / reference * _BPS


def _require_symbol_for_market(
    value: str,
    market: TossOrderMarket,
) -> None:
    _require_symbol(value)
    if market is TossOrderMarket.KR and _KR_SYMBOL_PATTERN.fullmatch(value) is None:
        raise OrderWriteConfigurationError(
            "KR symbols must contain exactly six digits."
        )
    if market is TossOrderMarket.US and _US_SYMBOL_PATTERN.fullmatch(value) is None:
        raise OrderWriteConfigurationError("US symbol format is invalid.")


def _require_client_order_id(value: str) -> None:
    if _CLIENT_ORDER_ID_PATTERN.fullmatch(value) is None:
        raise OrderWriteConfigurationError(
            "clientOrderId must be 1-36 allowed characters."
        )


def _require_symbol(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise OrderWriteConfigurationError("Symbol cannot be blank.")
    if value != value.strip():
        raise OrderWriteConfigurationError(
            "Symbol cannot contain surrounding whitespace."
        )


def _require_currency(value: str) -> None:
    if value not in {"KRW", "USD"}:
        raise OrderWriteConfigurationError("Currency must be KRW or USD.")


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise OrderWriteConfigurationError(
            f"{field_name} must be timezone-aware."
        )


def _require_nonblank(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise OrderWriteConfigurationError(f"{field_name} cannot be blank.")


def _require_bool(value: bool, field_name: str) -> None:
    if not isinstance(value, bool):
        raise OrderWriteConfigurationError(f"{field_name} must be boolean.")


def _require_sha256(value: str, field_name: str) -> None:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise OrderWriteConfigurationError(
            f"{field_name} must be lowercase SHA-256 text."
        )


def _require_positive_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OrderWriteConfigurationError(
            f"{field_name} must be a positive integer."
        )


def _require_nonnegative_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OrderWriteConfigurationError(
            f"{field_name} must be a nonnegative integer."
        )


def _require_finite_decimal(value: Decimal, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise OrderWriteConfigurationError(
            f"{field_name} must be a finite Decimal."
        )


def _require_positive_decimal(value: Decimal, field_name: str) -> None:
    _require_finite_decimal(value, field_name)
    if value <= _ZERO:
        raise OrderWriteConfigurationError(f"{field_name} must be positive.")


def _require_nonnegative_decimal(value: Decimal, field_name: str) -> None:
    _require_finite_decimal(value, field_name)
    if value < _ZERO:
        raise OrderWriteConfigurationError(
            f"{field_name} must be nonnegative."
        )


def _require_unit_interval(value: Decimal, field_name: str) -> None:
    _require_finite_decimal(value, field_name)
    if value < _ZERO or value >= Decimal("1"):
        raise OrderWriteConfigurationError(
            f"{field_name} must be in [0, 1)."
        )


__all__ = [
    "CompiledTossOrderRequest",
    "DryRunAccountState",
    "DryRunCheck",
    "DryRunDecision",
    "DryRunMarketState",
    "DryRunPosition",
    "DryRunTransportOutcome",
    "DryRunTransportReceipt",
    "HumanApprovalChallenge",
    "OFFICIAL_CLIENT_ORDER_ID_TTL_SECONDS",
    "OFFICIAL_HIGH_VALUE_THRESHOLD_KRW",
    "OFFICIAL_ORDER_CREATE_METHOD",
    "OFFICIAL_ORDER_CREATE_PATH",
    "OFFICIAL_TOSS_BASE_URL",
    "OrderWriteCertificationError",
    "OrderWriteConfigurationError",
    "OrderWriteDryRunReport",
    "OrderWriteIntegrityError",
    "OrderWriteSafetyError",
    "PINNED_OPENAPI_VERSION",
    "ReadOnlyCertificationEvidence",
    "TossOrderCreateContract",
    "TossOrderMarket",
    "TossOrderWritePolicy",
    "TossTimeInForce",
    "decimal_from",
    "decimal_text",
    "format_utc",
    "parse_utc_datetime",
    "price_deviation_bps",
    "sha256_document",
    "validate_price_precision",
]
