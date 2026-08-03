from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid5

_SCHEMA_VERSION: Final[int] = 1
_REPORT_NAMESPACE: Final[UUID] = UUID("b0d5596c-bf1b-5eaf-b8af-afccecfb0795")
_SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


class CredentialIsolationError(Exception):
    """Base error for credential-isolation certification."""


class CredentialIsolationConfigurationError(CredentialIsolationError):
    """Raised when a policy or credential input is invalid."""


class CredentialIsolationSafetyError(CredentialIsolationError):
    """Raised when a fail-closed credential safety rule rejects use."""


class CredentialIsolationIntegrityError(CredentialIsolationError):
    """Raised when token-lease state or report identity is inconsistent."""


class SecretSource(StrEnum):
    ENVIRONMENT = "environment"
    FIXTURE = "fixture"
    MACOS_KEYCHAIN = "macos_keychain"


class CredentialPurpose(StrEnum):
    READ_ONLY_PREPARATION = "read_only_preparation"
    ORDER_WRITE_DRY_RUN = "order_write_dry_run"


class TokenLeaseState(StrEnum):
    ISSUED = "issued"
    ACTIVE = "active"
    EXPIRING = "expiring"
    EXPIRED = "expired"
    REVOKED = "revoked"
    DESTROYED = "destroyed"


class CredentialCertificationDecision(StrEnum):
    PASSED = "passed"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class CredentialIsolationPolicy:
    provider: str = "toss"
    permitted_sources: tuple[SecretSource, ...] = (
        SecretSource.ENVIRONMENT,
        SecretSource.FIXTURE,
        SecretSource.MACOS_KEYCHAIN,
    )
    permitted_purposes: tuple[CredentialPurpose, ...] = (
        CredentialPurpose.READ_ONLY_PREPARATION,
        CredentialPurpose.ORDER_WRITE_DRY_RUN,
    )
    maximum_token_ttl_seconds: int = 86_400
    expiring_window_seconds: int = 300
    require_normal_kill_switch: bool = True
    external_network_enabled: bool = False
    broker_writes_enabled: bool = False
    secret_persistence_enabled: bool = False

    def __post_init__(self) -> None:
        _require_nonblank(self.provider, "Provider")
        if not self.permitted_sources:
            raise CredentialIsolationConfigurationError(
                "At least one secret source must be permitted."
            )
        if not self.permitted_purposes:
            raise CredentialIsolationConfigurationError(
                "At least one credential purpose must be permitted."
            )
        _require_positive_int(
            self.maximum_token_ttl_seconds,
            "Maximum token TTL seconds",
        )
        _require_nonnegative_int(
            self.expiring_window_seconds,
            "Expiring window seconds",
        )
        if self.expiring_window_seconds >= self.maximum_token_ttl_seconds:
            raise CredentialIsolationConfigurationError(
                "Expiring window must be shorter than maximum token TTL."
            )
        if not self.require_normal_kill_switch:
            raise CredentialIsolationConfigurationError(
                "Credential isolation v1 requires a normal kill switch."
            )
        if self.external_network_enabled:
            raise CredentialIsolationSafetyError(
                "External network access must remain disabled."
            )
        if self.broker_writes_enabled:
            raise CredentialIsolationSafetyError(
                "Broker writes must remain disabled."
            )
        if self.secret_persistence_enabled:
            raise CredentialIsolationSafetyError(
                "Secret persistence must remain disabled."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "permitted_sources": [value.value for value in self.permitted_sources],
            "permitted_purposes": [
                value.value for value in self.permitted_purposes
            ],
            "maximum_token_ttl_seconds": self.maximum_token_ttl_seconds,
            "expiring_window_seconds": self.expiring_window_seconds,
            "require_normal_kill_switch": self.require_normal_kill_switch,
            "external_network_enabled": False,
            "broker_writes_enabled": False,
            "secret_persistence_enabled": False,
        }


@dataclass(frozen=True, slots=True)
class CredentialFingerprint:
    provider: str
    source: SecretSource
    account_fingerprint: str
    client_id_fingerprint: str
    credential_version_fingerprint: str
    loaded_at: datetime

    def __post_init__(self) -> None:
        _require_nonblank(self.provider, "Provider")
        _require_sha256(self.account_fingerprint, "Account fingerprint")
        _require_sha256(self.client_id_fingerprint, "Client ID fingerprint")
        _require_sha256(
            self.credential_version_fingerprint,
            "Credential version fingerprint",
        )
        _require_aware(self.loaded_at, "Loaded timestamp")

    def to_document(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "source": self.source.value,
            "account_fingerprint": self.account_fingerprint,
            "client_id_fingerprint": self.client_id_fingerprint,
            "credential_version_fingerprint": (
                self.credential_version_fingerprint
            ),
            "loaded_at": format_utc(self.loaded_at),
        }


@dataclass(frozen=True, slots=True)
class TokenLeaseMetadata:
    lease_id: str
    token_fingerprint: str
    account_fingerprint: str
    client_id_fingerprint: str
    purpose: CredentialPurpose
    issued_at: datetime
    expires_at: datetime
    state: TokenLeaseState
    use_count: int

    def __post_init__(self) -> None:
        _require_nonblank(self.lease_id, "Lease ID")
        _require_sha256(self.token_fingerprint, "Token fingerprint")
        _require_sha256(self.account_fingerprint, "Account fingerprint")
        _require_sha256(self.client_id_fingerprint, "Client ID fingerprint")
        _require_aware(self.issued_at, "Issued timestamp")
        _require_aware(self.expires_at, "Expiration timestamp")
        if self.expires_at <= self.issued_at:
            raise CredentialIsolationConfigurationError(
                "Token expiration must follow issuance."
            )
        _require_nonnegative_int(self.use_count, "Token use count")

    def to_document(self) -> dict[str, object]:
        return {
            "lease_id": self.lease_id,
            "token_fingerprint": self.token_fingerprint,
            "account_fingerprint": self.account_fingerprint,
            "client_id_fingerprint": self.client_id_fingerprint,
            "purpose": self.purpose.value,
            "issued_at": format_utc(self.issued_at),
            "expires_at": format_utc(self.expires_at),
            "state": self.state.value,
            "use_count": self.use_count,
        }


@dataclass(frozen=True, slots=True)
class TokenUseReceipt:
    lease_id: str
    token_fingerprint: str
    purpose: CredentialPurpose
    used_at: datetime
    authorization_header: str
    network_call_count: int = 0
    broker_write_count: int = 0

    def __post_init__(self) -> None:
        _require_nonblank(self.lease_id, "Lease ID")
        _require_sha256(self.token_fingerprint, "Token fingerprint")
        _require_aware(self.used_at, "Token-use timestamp")
        if self.authorization_header != "Bearer [REDACTED]":
            raise CredentialIsolationSafetyError(
                "Authorization header must remain redacted."
            )
        if self.network_call_count != 0:
            raise CredentialIsolationSafetyError(
                "Credential certification cannot perform network calls."
            )
        if self.broker_write_count != 0:
            raise CredentialIsolationSafetyError(
                "Credential certification cannot perform broker writes."
            )

    def to_document(self) -> dict[str, object]:
        return {
            "lease_id": self.lease_id,
            "token_fingerprint": self.token_fingerprint,
            "purpose": self.purpose.value,
            "used_at": format_utc(self.used_at),
            "authorization_header": self.authorization_header,
            "network_call_count": 0,
            "broker_write_count": 0,
        }


@dataclass(frozen=True, slots=True)
class CredentialIsolationCheck:
    name: str
    passed: bool
    detail: str

    def __post_init__(self) -> None:
        _require_nonblank(self.name, "Check name")
        if not isinstance(self.passed, bool):
            raise CredentialIsolationConfigurationError(
                "Check result must be boolean."
            )
        _require_nonblank(self.detail, "Check detail")

    def to_document(self) -> dict[str, object]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class CredentialIsolationReport:
    evaluated_at: datetime
    policy: CredentialIsolationPolicy
    credential: CredentialFingerprint
    purpose: CredentialPurpose
    lease: TokenLeaseMetadata
    use_receipt: TokenUseReceipt
    checks: tuple[CredentialIsolationCheck, ...]
    decision: CredentialCertificationDecision
    source_secrets_destroyed: bool
    token_secret_destroyed: bool
    external_network_enabled: bool = False
    broker_write_count: int = 0
    secret_persistence_enabled: bool = False
    live_trading_enabled: bool = False
    schema_version: int = _SCHEMA_VERSION
    report_id: str = field(init=False)
    report_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _require_aware(self.evaluated_at, "Evaluation timestamp")
        if not self.checks:
            raise CredentialIsolationConfigurationError(
                "Credential report requires at least one check."
            )
        all_passed = all(check.passed for check in self.checks)
        expected = (
            CredentialCertificationDecision.PASSED
            if all_passed
            else CredentialCertificationDecision.REJECTED
        )
        if self.decision is not expected:
            raise CredentialIsolationIntegrityError(
                "Credential decision does not match checks."
            )
        if not self.source_secrets_destroyed or not self.token_secret_destroyed:
            raise CredentialIsolationSafetyError(
                "Credential certification requires secret destruction."
            )
        if self.lease.state is not TokenLeaseState.DESTROYED:
            raise CredentialIsolationSafetyError(
                "Persisted lease metadata must be destroyed."
            )
        if self.external_network_enabled:
            raise CredentialIsolationSafetyError(
                "External network access must remain disabled."
            )
        if self.broker_write_count != 0:
            raise CredentialIsolationSafetyError(
                "Broker write count must remain zero."
            )
        if self.secret_persistence_enabled:
            raise CredentialIsolationSafetyError(
                "Secret persistence must remain disabled."
            )
        if self.live_trading_enabled:
            raise CredentialIsolationSafetyError(
                "Live trading must remain disabled."
            )
        digest = sha256_document(self._identity_document())
        object.__setattr__(self, "report_digest", digest)
        object.__setattr__(self, "report_id", str(uuid5(_REPORT_NAMESPACE, digest)))

    def _identity_document(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "evaluated_at": format_utc(self.evaluated_at),
            "policy": self.policy.to_document(),
            "credential": self.credential.to_document(),
            "purpose": self.purpose.value,
            "lease": self.lease.to_document(),
            "use_receipt": self.use_receipt.to_document(),
            "checks": [check.to_document() for check in self.checks],
            "decision": self.decision.value,
            "source_secrets_destroyed": self.source_secrets_destroyed,
            "token_secret_destroyed": self.token_secret_destroyed,
            "external_network_enabled": False,
            "broker_write_count": 0,
            "secret_persistence_enabled": False,
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


def sha256_secret(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def format_utc(value: datetime) -> str:
    _require_aware(value, "Timestamp")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_utc_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise CredentialIsolationConfigurationError(
            f"{field_name} must be an ISO-8601 timestamp."
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CredentialIsolationConfigurationError(
            f"{field_name} must be an ISO-8601 timestamp."
        ) from error
    _require_aware(parsed, field_name)
    return parsed.astimezone(UTC)


def _require_nonblank(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CredentialIsolationConfigurationError(
            f"{field_name} cannot be blank."
        )


def _require_positive_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CredentialIsolationConfigurationError(
            f"{field_name} must be a positive integer."
        )


def _require_nonnegative_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CredentialIsolationConfigurationError(
            f"{field_name} must be a nonnegative integer."
        )


def _require_sha256(value: str, field_name: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise CredentialIsolationConfigurationError(
            f"{field_name} must be lowercase SHA-256 text."
        )


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CredentialIsolationConfigurationError(
            f"{field_name} must be timezone-aware."
        )
