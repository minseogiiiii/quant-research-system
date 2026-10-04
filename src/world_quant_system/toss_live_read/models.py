from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid5

OFFICIAL_TOSS_BASE_URL: Final[str] = "https://openapi.tossinvest.com"
OFFICIAL_TOSS_HOST: Final[str] = "openapi.tossinvest.com"
OFFICIAL_TOSS_PORT: Final[int] = 443
OFFICIAL_OPENAPI_VERSION: Final[str] = "1.2.9"
OFFICIAL_TOKEN_PATH: Final[str] = "/oauth2/token"
ACCOUNTS_PATH: Final[str] = "/api/v1/accounts"
HOLDINGS_PATH: Final[str] = "/api/v1/holdings"
ORDERS_PATH: Final[str] = "/api/v1/orders"
LIVE_READ_ENABLE_VARIABLE: Final[str] = "WQS_ENABLE_TOSS_LIVE_READ_ONLY"
LIVE_READ_ENABLE_VALUE: Final[str] = "YES"

_SCHEMA_VERSION: Final[int] = 1
_REPORT_NAMESPACE: Final[UUID] = UUID("28618cb8-b5e6-5162-9ee7-e5500c67592b")
_SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


class TossLiveReadError(Exception):
    """Base error for Toss live read-only certification."""


class TossLiveReadConfigurationError(TossLiveReadError):
    """Raised when policy or input configuration is invalid."""


class TossLiveReadSafetyError(TossLiveReadError):
    """Raised when a fail-closed live-read safety rule rejects execution."""


class TossLiveReadTransportError(TossLiveReadError):
    """Raised when a permitted live network operation fails."""


class TossLiveReadIntegrityError(TossLiveReadError):
    """Raised when broker data violates an internal invariant."""


class LiveReadOperation(StrEnum):
    ACCOUNTS = "accounts"
    HOLDINGS = "holdings"
    OPEN_ORDERS = "open_orders"
    CLOSED_ORDERS = "closed_orders"


class LiveReadDecision(StrEnum):
    CERTIFIED_READ_ONLY = "certified_read_only"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class TossLiveReadPolicy:
    provider: str = "toss"
    base_url: str = OFFICIAL_TOSS_BASE_URL
    pinned_openapi_version: str = OFFICIAL_OPENAPI_VERSION
    required_account_type: str = "BROKERAGE"
    timeout_seconds: float = 10.0
    maximum_response_bytes: int = 2_000_000
    maximum_read_attempts: int = 3
    maximum_retry_delay_seconds: float = 5.0
    closed_order_lookback_days: int = 30
    closed_order_page_size: int = 100
    maximum_closed_order_pages: int = 20
    enable_variable: str = LIVE_READ_ENABLE_VARIABLE
    enable_value: str = LIVE_READ_ENABLE_VALUE
    redirects_enabled: bool = False
    proxy_inheritance_enabled: bool = False
    broker_writes_enabled: bool = False
    token_persistence_enabled: bool = False
    account_persistence_enabled: bool = False

    def __post_init__(self) -> None:
        _require_nonblank(self.provider, "Provider")
        if self.provider.casefold() != "toss":
            raise TossLiveReadSafetyError("Only the Toss provider is supported.")
        if self.base_url != OFFICIAL_TOSS_BASE_URL:
            raise TossLiveReadSafetyError("The official Toss base URL is required.")
        if self.pinned_openapi_version != OFFICIAL_OPENAPI_VERSION:
            raise TossLiveReadSafetyError("Unexpected Toss OpenAPI version pin.")
        if self.required_account_type != "BROKERAGE":
            raise TossLiveReadSafetyError("Only BROKERAGE accounts are supported.")
        if not isinstance(self.timeout_seconds, (int, float)):
            raise TossLiveReadConfigurationError("Timeout must be numeric.")
        if not 0 < float(self.timeout_seconds) <= 60:
            raise TossLiveReadConfigurationError(
                "Timeout must be greater than zero and at most 60 seconds."
            )
        _require_positive_int(self.maximum_response_bytes, "Maximum response bytes")
        _require_positive_int(self.maximum_read_attempts, "Maximum read attempts")
        if self.maximum_read_attempts > 5:
            raise TossLiveReadSafetyError("Read retry attempts cannot exceed five.")
        if not isinstance(self.maximum_retry_delay_seconds, (int, float)):
            raise TossLiveReadConfigurationError("Retry delay must be numeric.")
        if not 0 <= float(self.maximum_retry_delay_seconds) <= 30:
            raise TossLiveReadConfigurationError(
                "Maximum retry delay must be between zero and 30 seconds."
            )
        if not 1 <= self.closed_order_lookback_days <= 365:
            raise TossLiveReadConfigurationError(
                "Closed-order lookback days must be between 1 and 365."
            )
        if not 1 <= self.closed_order_page_size <= 100:
            raise TossLiveReadConfigurationError(
                "Closed-order page size must be between 1 and 100."
            )
        if not 1 <= self.maximum_closed_order_pages <= 100:
            raise TossLiveReadConfigurationError(
                "Maximum closed-order pages must be between 1 and 100."
            )
        if self.enable_variable != LIVE_READ_ENABLE_VARIABLE:
            raise TossLiveReadSafetyError(
                "Live-read enable variable cannot be changed."
            )
        if self.enable_value != LIVE_READ_ENABLE_VALUE:
            raise TossLiveReadSafetyError(
                "Live-read enable value cannot be changed."
            )
        if self.redirects_enabled:
            raise TossLiveReadSafetyError("HTTP redirects must remain disabled.")
        if self.proxy_inheritance_enabled:
            raise TossLiveReadSafetyError("Proxy inheritance must remain disabled.")
        if self.broker_writes_enabled:
            raise TossLiveReadSafetyError("Broker writes must remain disabled.")
        if self.token_persistence_enabled:
            raise TossLiveReadSafetyError("Token persistence must remain disabled.")
        if self.account_persistence_enabled:
            raise TossLiveReadSafetyError(
                "Raw account persistence must remain disabled."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "pinned_openapi_version": self.pinned_openapi_version,
            "required_account_type": self.required_account_type,
            "timeout_seconds": float(self.timeout_seconds),
            "maximum_response_bytes": self.maximum_response_bytes,
            "maximum_read_attempts": self.maximum_read_attempts,
            "maximum_retry_delay_seconds": float(
                self.maximum_retry_delay_seconds
            ),
            "closed_order_lookback_days": self.closed_order_lookback_days,
            "closed_order_page_size": self.closed_order_page_size,
            "maximum_closed_order_pages": self.maximum_closed_order_pages,
            "enable_variable": self.enable_variable,
            "enable_value": self.enable_value,
            "redirects_enabled": False,
            "proxy_inheritance_enabled": False,
            "broker_writes_enabled": False,
            "token_persistence_enabled": False,
            "account_persistence_enabled": False,
        }


@dataclass(frozen=True, slots=True)
class TossHttpResponse:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    json_body: object

    def __post_init__(self) -> None:
        if not isinstance(self.status_code, int) or not 100 <= self.status_code <= 599:
            raise TossLiveReadConfigurationError("HTTP status code is invalid.")
        for name, value in self.headers:
            _require_nonblank(name, "Response header name")
            if not isinstance(value, str):
                raise TossLiveReadConfigurationError(
                    "Response header value must be text."
                )

    def header(self, name: str) -> str | None:
        expected = name.casefold()
        for header_name, value in self.headers:
            if header_name.casefold() == expected:
                return value
        return None


@dataclass(frozen=True, slots=True)
class AccountEvidence:
    account_fingerprint: str
    account_type: str
    returned_account_count: int
    payload_digest: str

    def __post_init__(self) -> None:
        _require_sha256(self.account_fingerprint, "Account fingerprint")
        _require_nonblank(self.account_type, "Account type")
        _require_nonnegative_int(
            self.returned_account_count,
            "Returned account count",
        )
        _require_sha256(self.payload_digest, "Account payload digest")

    def to_document(self) -> dict[str, object]:
        return {
            "account_fingerprint": self.account_fingerprint,
            "account_type": self.account_type,
            "returned_account_count": self.returned_account_count,
            "payload_digest": self.payload_digest,
        }


@dataclass(frozen=True, slots=True)
class CollectionEvidence:
    operation: LiveReadOperation
    item_count: int
    page_count: int
    content_digest: str
    observed_states: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_nonnegative_int(self.item_count, "Item count")
        _require_positive_int(self.page_count, "Page count")
        _require_sha256(self.content_digest, "Collection content digest")
        if tuple(sorted(set(self.observed_states))) != self.observed_states:
            raise TossLiveReadConfigurationError(
                "Observed states must be sorted and unique."
            )
        for state in self.observed_states:
            _require_nonblank(state, "Observed state")

    def to_document(self) -> dict[str, object]:
        return {
            "operation": self.operation.value,
            "item_count": self.item_count,
            "page_count": self.page_count,
            "content_digest": self.content_digest,
            "observed_states": list(self.observed_states),
        }


@dataclass(frozen=True, slots=True)
class RateLimitEvidence:
    observation_count: int
    minimum_remaining: int | None
    maximum_limit: int | None
    maximum_retry_after_seconds: int | None

    def __post_init__(self) -> None:
        _require_nonnegative_int(self.observation_count, "Observation count")
        for value, name in (
            (self.minimum_remaining, "Minimum remaining"),
            (self.maximum_limit, "Maximum limit"),
            (self.maximum_retry_after_seconds, "Maximum retry-after seconds"),
        ):
            if value is not None:
                _require_nonnegative_int(value, name)

    def to_document(self) -> dict[str, object]:
        return {
            "observation_count": self.observation_count,
            "minimum_remaining": self.minimum_remaining,
            "maximum_limit": self.maximum_limit,
            "maximum_retry_after_seconds": self.maximum_retry_after_seconds,
        }


@dataclass(frozen=True, slots=True)
class LiveReadCheck:
    name: str
    passed: bool
    detail: str

    def __post_init__(self) -> None:
        _require_nonblank(self.name, "Check name")
        if not isinstance(self.passed, bool):
            raise TossLiveReadConfigurationError("Check result must be boolean.")
        _require_nonblank(self.detail, "Check detail")

    def to_document(self) -> dict[str, object]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class TossLiveReadCertificationReport:
    provider: str
    decision: LiveReadDecision
    generated_at: datetime
    policy: TossLiveReadPolicy
    account: AccountEvidence
    holdings: CollectionEvidence
    open_orders: CollectionEvidence
    closed_orders: CollectionEvidence
    rate_limits: RateLimitEvidence
    checks: tuple[LiveReadCheck, ...]
    token_fingerprint: str
    auth_network_call_count: int
    read_network_call_count: int
    broker_write_count: int = 0
    order_submission_enabled: bool = False
    report_id: str = field(init=False)
    report_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _require_nonblank(self.provider, "Provider")
        _require_aware(self.generated_at, "Generated timestamp")
        _require_sha256(self.token_fingerprint, "Token fingerprint")
        _require_nonnegative_int(
            self.auth_network_call_count,
            "Auth network call count",
        )
        _require_nonnegative_int(
            self.read_network_call_count,
            "Read network call count",
        )
        if self.auth_network_call_count != 1:
            raise TossLiveReadSafetyError(
                "Live certification requires exactly one OAuth token request."
            )
        if self.read_network_call_count < 4:
            raise TossLiveReadSafetyError(
                "Live certification requires all four read-only operations."
            )
        if self.broker_write_count != 0:
            raise TossLiveReadSafetyError("Broker write count must remain zero.")
        if self.order_submission_enabled:
            raise TossLiveReadSafetyError("Order submission must remain disabled.")
        if not self.checks or not all(check.passed for check in self.checks):
            raise TossLiveReadIntegrityError(
                "A certified report requires all safety checks to pass."
            )
        if self.decision is not LiveReadDecision.CERTIFIED_READ_ONLY:
            raise TossLiveReadIntegrityError(
                "Successful report decision must be CERTIFIED_READ_ONLY."
            )
        identity = canonical_json(self._identity_document())
        report_digest = hashlib.sha256(identity).hexdigest()
        object.__setattr__(self, "report_digest", report_digest)
        object.__setattr__(
            self,
            "report_id",
            str(uuid5(_REPORT_NAMESPACE, report_digest)),
        )

    def _identity_document(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "provider": self.provider,
            "decision": self.decision.value,
            "generated_at": format_utc(self.generated_at),
            "policy": self.policy.to_document(),
            "account": self.account.to_document(),
            "holdings": self.holdings.to_document(),
            "open_orders": self.open_orders.to_document(),
            "closed_orders": self.closed_orders.to_document(),
            "rate_limits": self.rate_limits.to_document(),
            "checks": [check.to_document() for check in self.checks],
            "token_fingerprint": self.token_fingerprint,
            "auth_network_call_count": self.auth_network_call_count,
            "read_network_call_count": self.read_network_call_count,
            "broker_write_count": 0,
            "order_submission_enabled": False,
        }

    def to_document(self) -> dict[str, object]:
        return {
            **self._identity_document(),
            "report_id": self.report_id,
            "report_digest": self.report_digest,
        }


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()


def digest_document(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def sha256_secret(value: bytes) -> str:
    if not isinstance(value, bytes) or not value:
        raise TossLiveReadConfigurationError(
            "Secret fingerprint input must be nonempty bytes."
        )
    return hashlib.sha256(value).hexdigest()


def format_utc(value: datetime) -> str:
    _require_aware(value, "Timestamp")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _require_nonblank(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise TossLiveReadConfigurationError(f"{field_name} cannot be blank.")


def _require_positive_int(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TossLiveReadConfigurationError(
            f"{field_name} must be a positive integer."
        )


def _require_nonnegative_int(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TossLiveReadConfigurationError(
            f"{field_name} must be a nonnegative integer."
        )


def _require_sha256(value: object, field_name: str) -> None:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise TossLiveReadConfigurationError(
            f"{field_name} must be a lowercase SHA-256 digest."
        )


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TossLiveReadConfigurationError(
            f"{field_name} must be timezone-aware."
        )
