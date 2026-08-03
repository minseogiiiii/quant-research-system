from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID, uuid5

_SCHEMA_VERSION: Final[int] = 1
_REPORT_NAMESPACE: Final[UUID] = UUID("e560c31f-0ddf-5238-90cb-a89485ec21d9")
_OFFICIAL_BASE_URL: Final[str] = "https://openapi.tossinvest.com"
_ZERO_HASH: Final[str] = "0" * 64


class BrokerAdapterCertificationError(Exception):
    """Base exception for adapter-certification failures."""


class BrokerAdapterConfigurationError(BrokerAdapterCertificationError):
    """Raised when certification configuration is invalid."""


class BrokerAdapterSafetyError(BrokerAdapterCertificationError):
    """Raised when a read-only safety boundary is violated."""


class BrokerAdapterIntegrityError(BrokerAdapterCertificationError):
    """Raised when evidence or response state is inconsistent."""


class CertificationStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"


class FindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ReadOnlyOperation(StrEnum):
    ACCOUNTS = "accounts"
    HOLDINGS = "holdings"
    OPEN_ORDERS = "open_orders"
    CLOSED_ORDERS = "closed_orders"


class ReadOnlyHttpMethod(StrEnum):
    GET = "GET"


@dataclass(frozen=True, slots=True)
class ReadOnlyEndpointSpec:
    operation: ReadOnlyOperation
    path: str
    account_header_required: bool
    rate_limit_group: str
    fixed_query: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.operation, ReadOnlyOperation):
            raise BrokerAdapterConfigurationError("Read-only operation is invalid.")
        _require_api_path(self.path)
        _require_bool(self.account_header_required, "Account-header requirement")
        _require_nonblank(self.rate_limit_group, "Rate-limit group")
        normalized_query: list[tuple[str, str]] = []
        seen_keys: set[str] = set()
        for key, value in self.fixed_query:
            _require_nonblank(key, "Fixed-query key")
            _require_nonblank(value, "Fixed-query value")
            if key in seen_keys:
                raise BrokerAdapterConfigurationError(
                    f"Duplicate fixed-query key: {key}"
                )
            seen_keys.add(key)
            normalized_query.append((key, value))
        if tuple(normalized_query) != self.fixed_query:
            raise BrokerAdapterConfigurationError(
                "Fixed-query parameters must already be normalized."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "operation": self.operation.value,
            "method": ReadOnlyHttpMethod.GET.value,
            "path": self.path,
            "account_header_required": self.account_header_required,
            "rate_limit_group": self.rate_limit_group,
            "fixed_query": [[key, value] for key, value in self.fixed_query],
        }


@dataclass(frozen=True, slots=True)
class ReadOnlyAdapterPolicy:
    provider: str
    base_url: str
    expected_account_fingerprint: str
    required_operations: tuple[ReadOnlyOperation, ...]
    maximum_response_bytes: int = 2_000_000
    maximum_pages_per_operation: int = 100
    maximum_attempts: int = 3
    base_backoff_seconds: float = 0.25
    maximum_backoff_seconds: float = 2.0
    allowed_clock_skew_seconds: int = 300
    require_request_id: bool = True

    def __post_init__(self) -> None:
        _require_nonblank(self.provider, "Broker provider")
        if self.base_url != _OFFICIAL_BASE_URL:
            raise BrokerAdapterSafetyError(
                "Read-only certification only permits the official Toss Open API host."
            )
        _require_sha256(
            self.expected_account_fingerprint,
            "Expected account fingerprint",
        )
        if not self.required_operations:
            raise BrokerAdapterConfigurationError(
                "At least one read-only operation is required."
            )
        if len(set(self.required_operations)) != len(self.required_operations):
            raise BrokerAdapterConfigurationError(
                "Required operations cannot contain duplicates."
            )
        for operation in self.required_operations:
            if operation not in _ENDPOINT_BY_OPERATION:
                raise BrokerAdapterConfigurationError(
                    f"Unsupported read-only operation: {operation!r}"
                )
        _require_positive_int(self.maximum_response_bytes, "Maximum response bytes")
        _require_positive_int(
            self.maximum_pages_per_operation,
            "Maximum pages per operation",
        )
        _require_positive_int(self.maximum_attempts, "Maximum attempts")
        _require_nonnegative_float(
            self.base_backoff_seconds,
            "Base backoff seconds",
        )
        _require_nonnegative_float(
            self.maximum_backoff_seconds,
            "Maximum backoff seconds",
        )
        if self.maximum_backoff_seconds < self.base_backoff_seconds:
            raise BrokerAdapterConfigurationError(
                "Maximum backoff cannot be below base backoff."
            )
        _require_nonnegative_int(
            self.allowed_clock_skew_seconds,
            "Allowed clock skew seconds",
        )
        _require_bool(self.require_request_id, "Request-ID requirement")

    @property
    def policy_digest(self) -> str:
        return sha256_document(self.to_document())

    def endpoint_for(self, operation: ReadOnlyOperation) -> ReadOnlyEndpointSpec:
        if operation not in self.required_operations:
            raise BrokerAdapterSafetyError(
                f"Operation is not enabled by policy: {operation.value}"
            )
        return _ENDPOINT_BY_OPERATION[operation]

    def to_document(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "expected_account_fingerprint": self.expected_account_fingerprint,
            "required_operations": [
                operation.value for operation in self.required_operations
            ],
            "maximum_response_bytes": self.maximum_response_bytes,
            "maximum_pages_per_operation": self.maximum_pages_per_operation,
            "maximum_attempts": self.maximum_attempts,
            "base_backoff_seconds": self.base_backoff_seconds,
            "maximum_backoff_seconds": self.maximum_backoff_seconds,
            "allowed_clock_skew_seconds": self.allowed_clock_skew_seconds,
            "require_request_id": self.require_request_id,
        }


@dataclass(frozen=True, slots=True)
class ReadOnlyRequest:
    operation: ReadOnlyOperation
    url: str
    headers: tuple[tuple[str, str], ...]
    method: ReadOnlyHttpMethod = ReadOnlyHttpMethod.GET
    body: bytes | None = None

    def __post_init__(self) -> None:
        if self.method is not ReadOnlyHttpMethod.GET:
            raise BrokerAdapterSafetyError("Certification requests must use GET.")
        if self.body is not None:
            raise BrokerAdapterSafetyError(
                "Read-only certification requests cannot contain a body."
            )
        endpoint = _ENDPOINT_BY_OPERATION.get(self.operation)
        if endpoint is None:
            raise BrokerAdapterSafetyError("Unknown read-only operation.")
        split = urlsplit(self.url)
        if split.scheme != "https" or split.netloc != "openapi.tossinvest.com":
            raise BrokerAdapterSafetyError(
                "Certification request host must be the official HTTPS endpoint."
            )
        if split.path != endpoint.path:
            raise BrokerAdapterSafetyError(
                f"Unexpected path for {self.operation.value}: {split.path}"
            )
        query = tuple(sorted(parse_qsl(split.query, keep_blank_values=True)))
        if query != tuple(sorted(endpoint.fixed_query)):
            raise BrokerAdapterSafetyError(
                f"Unexpected query for {self.operation.value}."
            )
        normalized_headers = _normalize_headers(self.headers)
        if "authorization" not in normalized_headers:
            raise BrokerAdapterSafetyError("Authorization header is required.")
        if not normalized_headers["authorization"].startswith("Bearer "):
            raise BrokerAdapterSafetyError("Authorization must use Bearer token.")
        has_account_header = "x-tossinvest-account" in normalized_headers
        if endpoint.account_header_required and not has_account_header:
            raise BrokerAdapterSafetyError(
                "Account-scoped operation requires X-Tossinvest-Account."
            )
        if not endpoint.account_header_required and has_account_header:
            raise BrokerAdapterSafetyError(
                "Account list request must not include an account selection header."
            )
        for header_name in normalized_headers:
            if header_name in {"content-length", "content-type"}:
                raise BrokerAdapterSafetyError(
                    "GET certification requests cannot carry content headers."
                )

    @property
    def safe_document(self) -> dict[str, object]:
        normalized_headers = _normalize_headers(self.headers)
        safe_headers = {
            key: "<redacted>" if key == "authorization" else value
            for key, value in sorted(normalized_headers.items())
        }
        return {
            "operation": self.operation.value,
            "method": self.method.value,
            "url": self.url,
            "headers": safe_headers,
            "body_present": self.body is not None,
        }


@dataclass(frozen=True, slots=True)
class ReadOnlyResponse:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: dict[str, object]
    received_at: datetime
    raw_size_bytes: int

    def __post_init__(self) -> None:
        if isinstance(self.status_code, bool) or not isinstance(self.status_code, int):
            raise BrokerAdapterConfigurationError("HTTP status must be an integer.")
        if not 100 <= self.status_code <= 599:
            raise BrokerAdapterConfigurationError("HTTP status is out of range.")
        _normalize_headers(self.headers)
        if not isinstance(self.body, dict):
            raise BrokerAdapterConfigurationError("Response body must be an object.")
        _require_aware(self.received_at, "Response receipt time")
        _require_nonnegative_int(self.raw_size_bytes, "Raw response size")

    @property
    def normalized_headers(self) -> dict[str, str]:
        return _normalize_headers(self.headers)

    @property
    def response_digest(self) -> str:
        return sha256_document(
            {
                "status_code": self.status_code,
                "headers": dict(sorted(self.normalized_headers.items())),
                "body": self.body,
                "received_at": format_utc(self.received_at),
                "raw_size_bytes": self.raw_size_bytes,
            }
        )


@dataclass(frozen=True, slots=True)
class RateLimitObservation:
    limit: int | None
    remaining: int | None
    reset_seconds: float | None
    retry_after_seconds: float | None

    @classmethod
    def from_headers(cls, headers: dict[str, str]) -> RateLimitObservation:
        return cls(
            limit=_optional_nonnegative_int(headers.get("x-ratelimit-limit")),
            remaining=_optional_nonnegative_int(
                headers.get("x-ratelimit-remaining")
            ),
            reset_seconds=_optional_nonnegative_float(
                headers.get("x-ratelimit-reset")
            ),
            retry_after_seconds=_optional_nonnegative_float(
                headers.get("retry-after")
            ),
        )

    def to_document(self) -> dict[str, object]:
        return {
            "limit": self.limit,
            "remaining": self.remaining,
            "reset_seconds": self.reset_seconds,
            "retry_after_seconds": self.retry_after_seconds,
        }


@dataclass(frozen=True, slots=True)
class NormalizedAccount:
    account_seq: str
    account_name: str | None
    raw_digest: str

    def __post_init__(self) -> None:
        _require_nonblank(self.account_seq, "Account sequence")
        if self.account_name is not None:
            _require_nonblank(self.account_name, "Account name")
        _require_sha256(self.raw_digest, "Account raw digest")

    @property
    def account_fingerprint(self) -> str:
        return hashlib.sha256(self.account_seq.encode()).hexdigest()

    def to_document(self) -> dict[str, object]:
        return {
            "account_seq": self.account_seq,
            "account_name": self.account_name,
            "account_fingerprint": self.account_fingerprint,
            "raw_digest": self.raw_digest,
        }


@dataclass(frozen=True, slots=True)
class NormalizedHolding:
    symbol: str
    quantity: str
    average_price: str | None
    currency: str | None
    raw_digest: str

    def __post_init__(self) -> None:
        _require_symbol(self.symbol)
        _require_nonblank(self.quantity, "Holding quantity")
        if self.average_price is not None:
            _require_nonblank(self.average_price, "Average price")
        if self.currency is not None:
            _require_currency(self.currency)
        _require_sha256(self.raw_digest, "Holding raw digest")

    def to_document(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "average_price": self.average_price,
            "currency": self.currency,
            "raw_digest": self.raw_digest,
        }


@dataclass(frozen=True, slots=True)
class NormalizedOrder:
    order_id: str
    client_order_id: str | None
    symbol: str
    side: str
    status: str
    quantity: str
    filled_quantity: str | None
    raw_digest: str

    def __post_init__(self) -> None:
        _require_nonblank(self.order_id, "Order ID")
        if self.client_order_id is not None:
            _require_nonblank(self.client_order_id, "Client order ID")
        _require_symbol(self.symbol)
        _require_nonblank(self.side, "Order side")
        _require_nonblank(self.status, "Order status")
        _require_nonblank(self.quantity, "Order quantity")
        if self.filled_quantity is not None:
            _require_nonblank(self.filled_quantity, "Filled quantity")
        _require_sha256(self.raw_digest, "Order raw digest")

    def to_document(self) -> dict[str, object]:
        return {
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "status": self.status,
            "quantity": self.quantity,
            "filled_quantity": self.filled_quantity,
            "raw_digest": self.raw_digest,
        }


@dataclass(frozen=True, slots=True)
class CertificationFinding:
    code: str
    severity: FindingSeverity
    message: str
    operation: ReadOnlyOperation | None = None

    def __post_init__(self) -> None:
        _require_nonblank(self.code, "Finding code")
        if not isinstance(self.severity, FindingSeverity):
            raise BrokerAdapterConfigurationError("Finding severity is invalid.")
        _require_nonblank(self.message, "Finding message")
        if self.operation is not None and not isinstance(
            self.operation,
            ReadOnlyOperation,
        ):
            raise BrokerAdapterConfigurationError("Finding operation is invalid.")

    def to_document(self) -> dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "operation": None if self.operation is None else self.operation.value,
        }


@dataclass(frozen=True, slots=True)
class OperationEvidence:
    operation: ReadOnlyOperation
    request: ReadOnlyRequest
    response_digests: tuple[str, ...]
    page_count: int
    item_count: int
    request_ids: tuple[str, ...]
    rate_limits: tuple[RateLimitObservation, ...]

    def __post_init__(self) -> None:
        if self.operation != self.request.operation:
            raise BrokerAdapterIntegrityError(
                "Operation evidence does not match its request."
            )
        _require_positive_int(self.page_count, "Operation page count")
        if len(self.response_digests) != self.page_count:
            raise BrokerAdapterIntegrityError(
                "Response digest count must match page count."
            )
        if len(self.request_ids) != self.page_count:
            raise BrokerAdapterIntegrityError(
                "Request-ID count must match page count."
            )
        if len(self.rate_limits) != self.page_count:
            raise BrokerAdapterIntegrityError(
                "Rate-limit count must match page count."
            )
        for response_digest in self.response_digests:
            _require_sha256(response_digest, "Response digest")
        for request_id in self.request_ids:
            _require_nonblank(request_id, "Request ID")
        _require_nonnegative_int(self.item_count, "Operation item count")

    def to_document(self) -> dict[str, object]:
        return {
            "operation": self.operation.value,
            "request": self.request.safe_document,
            "response_digests": list(self.response_digests),
            "page_count": self.page_count,
            "item_count": self.item_count,
            "request_ids": list(self.request_ids),
            "rate_limits": [
                rate_limit.to_document() for rate_limit in self.rate_limits
            ],
        }


@dataclass(frozen=True, slots=True)
class AdapterCertificationReport:
    provider: str
    base_url: str
    certified_at: datetime
    policy_digest: str
    account_fingerprint: str
    status: CertificationStatus
    operations: tuple[OperationEvidence, ...]
    accounts: tuple[NormalizedAccount, ...]
    holdings: tuple[NormalizedHolding, ...]
    orders: tuple[NormalizedOrder, ...]
    findings: tuple[CertificationFinding, ...]
    network_transport_enabled: bool = False
    write_operations_enabled: bool = False
    schema_version: int = _SCHEMA_VERSION
    report_id: str = field(init=False)
    report_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _require_nonblank(self.provider, "Broker provider")
        if self.base_url != _OFFICIAL_BASE_URL:
            raise BrokerAdapterSafetyError("Unexpected certification base URL.")
        _require_aware(self.certified_at, "Certification time")
        _require_sha256(self.policy_digest, "Policy digest")
        _require_sha256(self.account_fingerprint, "Account fingerprint")
        if not isinstance(self.status, CertificationStatus):
            raise BrokerAdapterConfigurationError(
                "Certification status is invalid."
            )
        if len({operation.operation for operation in self.operations}) != len(
            self.operations
        ):
            raise BrokerAdapterIntegrityError(
                "Certification operations cannot contain duplicates."
            )
        _require_bool(
            self.network_transport_enabled,
            "Network-transport enabled flag",
        )
        _require_bool(
            self.write_operations_enabled,
            "Write-operations enabled flag",
        )
        if self.write_operations_enabled:
            raise BrokerAdapterSafetyError(
                "Adapter certification v1 cannot enable write operations."
            )
        _require_positive_int(self.schema_version, "Schema version")
        finding_severities = {finding.severity for finding in self.findings}
        if self.status is CertificationStatus.PASS and FindingSeverity.CRITICAL in (
            finding_severities
        ):
            raise BrokerAdapterIntegrityError(
                "Passing reports cannot contain critical findings."
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
            "schema_version": self.schema_version,
            "provider": self.provider,
            "base_url": self.base_url,
            "certified_at": format_utc(self.certified_at),
            "policy_digest": self.policy_digest,
            "account_fingerprint": self.account_fingerprint,
            "status": self.status.value,
            "operations": [operation.to_document() for operation in self.operations],
            "accounts": [account.to_document() for account in self.accounts],
            "holdings": [holding.to_document() for holding in self.holdings],
            "orders": [order.to_document() for order in self.orders],
            "findings": [finding.to_document() for finding in self.findings],
            "network_transport_enabled": self.network_transport_enabled,
            "write_operations_enabled": self.write_operations_enabled,
        }

    def to_document(self) -> dict[str, object]:
        return {
            **self._identity_document(),
            "report_id": self.report_id,
            "report_digest": self.report_digest,
        }


def endpoint_for(operation: ReadOnlyOperation) -> ReadOnlyEndpointSpec:
    try:
        return _ENDPOINT_BY_OPERATION[operation]
    except KeyError as error:
        raise BrokerAdapterConfigurationError(
            f"Unknown read-only operation: {operation!r}"
        ) from error


def sha256_document(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def format_utc(value: datetime) -> str:
    _require_aware(value, "Timestamp")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_utc_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise BrokerAdapterConfigurationError(
            f"{field_name} must be an ISO-8601 timestamp."
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise BrokerAdapterConfigurationError(
            f"{field_name} must be an ISO-8601 timestamp."
        ) from error
    _require_aware(parsed, field_name)
    return parsed.astimezone(UTC)


def _normalize_headers(headers: tuple[tuple[str, str], ...]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_name, raw_value in headers:
        _require_nonblank(raw_name, "Header name")
        _require_nonblank(raw_value, "Header value")
        name = raw_name.strip().lower()
        if name in normalized:
            raise BrokerAdapterConfigurationError(f"Duplicate header: {name}")
        normalized[name] = raw_value.strip()
    return normalized


def _optional_nonnegative_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value)
    except ValueError as error:
        raise BrokerAdapterConfigurationError(
            "Rate-limit integer header is invalid."
        ) from error
    _require_nonnegative_int(parsed, "Rate-limit integer header")
    return parsed


def _optional_nonnegative_float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = float(value)
    except ValueError as error:
        raise BrokerAdapterConfigurationError(
            "Rate-limit numeric header is invalid."
        ) from error
    _require_nonnegative_float(parsed, "Rate-limit numeric header")
    return parsed


def _require_api_path(value: str) -> None:
    if not value.startswith("/api/v1/"):
        raise BrokerAdapterConfigurationError(
            "Read-only endpoint path must stay under /api/v1/."
        )
    lowered = value.lower()
    forbidden_fragments = (
        "/modify",
        "/cancel",
        "conditional-orders",
        "/oauth2/",
    )
    if any(fragment in lowered for fragment in forbidden_fragments):
        raise BrokerAdapterSafetyError(
            "Read-only endpoint path contains a write or auth operation."
        )


def _require_symbol(value: str) -> None:
    _require_nonblank(value, "Symbol")
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.-")
    if any(character not in allowed for character in value):
        raise BrokerAdapterConfigurationError("Symbol contains invalid characters.")


def _require_currency(value: str) -> None:
    if not isinstance(value, str) or len(value) != 3 or not value.isalpha():
        raise BrokerAdapterConfigurationError(
            "Currency must be a three-letter alphabetic code."
        )


def _require_sha256(value: str, field_name: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise BrokerAdapterConfigurationError(
            f"{field_name} must be a SHA-256 hex digest."
        )
    try:
        int(value, 16)
    except ValueError as error:
        raise BrokerAdapterConfigurationError(
            f"{field_name} must be a SHA-256 hex digest."
        ) from error


def _require_nonblank(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise BrokerAdapterConfigurationError(f"{field_name} cannot be blank.")


def _require_bool(value: bool, field_name: str) -> None:
    if not isinstance(value, bool):
        raise BrokerAdapterConfigurationError(f"{field_name} must be boolean.")


def _require_positive_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BrokerAdapterConfigurationError(
            f"{field_name} must be a positive integer."
        )


def _require_nonnegative_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BrokerAdapterConfigurationError(
            f"{field_name} must be a nonnegative integer."
        )


def _require_nonnegative_float(value: float, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise BrokerAdapterConfigurationError(
            f"{field_name} must be a nonnegative number."
        )


def _require_aware(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise BrokerAdapterConfigurationError(
            f"{field_name} must be timezone-aware."
        )


OFFICIAL_READ_ONLY_ENDPOINTS: Final[tuple[ReadOnlyEndpointSpec, ...]] = (
    ReadOnlyEndpointSpec(
        operation=ReadOnlyOperation.ACCOUNTS,
        path="/api/v1/accounts",
        account_header_required=False,
        rate_limit_group="ACCOUNT",
    ),
    ReadOnlyEndpointSpec(
        operation=ReadOnlyOperation.HOLDINGS,
        path="/api/v1/holdings",
        account_header_required=True,
        rate_limit_group="ASSET",
    ),
    ReadOnlyEndpointSpec(
        operation=ReadOnlyOperation.OPEN_ORDERS,
        path="/api/v1/orders",
        account_header_required=True,
        rate_limit_group="ORDER_HISTORY",
        fixed_query=(("status", "OPEN"),),
    ),
    ReadOnlyEndpointSpec(
        operation=ReadOnlyOperation.CLOSED_ORDERS,
        path="/api/v1/orders",
        account_header_required=True,
        rate_limit_group="ORDER_HISTORY",
        fixed_query=(("status", "CLOSED"),),
    ),
)

_ENDPOINT_BY_OPERATION: Final[dict[ReadOnlyOperation, ReadOnlyEndpointSpec]] = {
    endpoint.operation: endpoint for endpoint in OFFICIAL_READ_ONLY_ENDPOINTS
}


ZERO_HASH: Final[str] = _ZERO_HASH
OFFICIAL_BASE_URL: Final[str] = _OFFICIAL_BASE_URL
