from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid5

from world_quant_system.credential_isolation.models import (
    CredentialPurpose,
    TokenLeaseMetadata,
    TokenLeaseState,
    TokenUseReceipt,
)
from world_quant_system.order_write_certification.models import (
    OFFICIAL_ORDER_CREATE_METHOD,
    OFFICIAL_ORDER_CREATE_PATH,
    OFFICIAL_TOSS_BASE_URL,
    CompiledTossOrderRequest,
    HumanApprovalChallenge,
)

_SCHEMA_VERSION: Final[int] = 1
_REPORT_NAMESPACE: Final[UUID] = UUID("1e68ceaa-349c-5e74-b69c-2ace55336191")
_GRANT_NAMESPACE: Final[UUID] = UUID("f7dd7a67-4d37-570c-9f15-39d3d9d0c633")
_ENVELOPE_NAMESPACE: Final[UUID] = UUID("dd9dcc2f-6f78-578d-bb39-ff9c435839d0")
_ATTEMPT_NAMESPACE: Final[UUID] = UUID("89f0fcf2-b211-555e-ad3e-7a29605abffe")
_SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


class DisabledWriteTransportError(Exception):
    """Base error for disabled write-transport integration."""


class DisabledWriteTransportConfigurationError(DisabledWriteTransportError):
    """Raised when transport evidence or policy is malformed."""


class DisabledWriteTransportSafetyError(DisabledWriteTransportError):
    """Raised when a fail-closed transport invariant is violated."""


class DisabledWriteTransportIntegrityError(DisabledWriteTransportError):
    """Raised when deterministic bindings or identities disagree."""


class ApprovalScope(StrEnum):
    ORDER_CREATE_ONCE = "order_create_once"


class TransportState(StrEnum):
    COMPILED = "compiled"
    APPROVAL_VALIDATED = "approval_validated"
    CREDENTIAL_VALIDATED = "credential_validated"
    TRANSPORT_BLOCKED = "transport_blocked"


class TransportOutcome(StrEnum):
    WRITE_TRANSPORT_DISABLED = "write_transport_disabled"


class RetryDisposition(StrEnum):
    NO_AUTOMATIC_RETRY = "no_automatic_retry"


class IntegrationDecision(StrEnum):
    CERTIFIED_BLOCKED = "certified_blocked"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class DisabledWriteTransportPolicy:
    base_url: str = OFFICIAL_TOSS_BASE_URL
    path: str = OFFICIAL_ORDER_CREATE_PATH
    method: str = OFFICIAL_ORDER_CREATE_METHOD
    required_purpose: CredentialPurpose = CredentialPurpose.ORDER_WRITE_DRY_RUN
    maximum_envelope_ttl_seconds: int = 120
    allow_redirects: bool = False
    automatic_retries_enabled: bool = False
    external_network_enabled: bool = False
    broker_writes_enabled: bool = False
    submission_states_enabled: bool = False

    def __post_init__(self) -> None:
        if self.base_url != OFFICIAL_TOSS_BASE_URL:
            raise DisabledWriteTransportSafetyError(
                "Disabled transport must pin the official Toss base URL."
            )
        if self.path != OFFICIAL_ORDER_CREATE_PATH:
            raise DisabledWriteTransportSafetyError(
                "Disabled transport must pin the order-create path."
            )
        if self.method != OFFICIAL_ORDER_CREATE_METHOD:
            raise DisabledWriteTransportSafetyError(
                "Disabled transport must pin the POST method."
            )
        if self.required_purpose is not CredentialPurpose.ORDER_WRITE_DRY_RUN:
            raise DisabledWriteTransportSafetyError(
                "Disabled transport requires the order-write dry-run purpose."
            )
        _require_positive_int(
            self.maximum_envelope_ttl_seconds,
            "Maximum envelope TTL seconds",
        )
        if self.allow_redirects:
            raise DisabledWriteTransportSafetyError(
                "Redirects must remain disabled for order writes."
            )
        if self.automatic_retries_enabled:
            raise DisabledWriteTransportSafetyError(
                "Automatic write retries must remain disabled."
            )
        if self.external_network_enabled:
            raise DisabledWriteTransportSafetyError(
                "External network transport must remain disabled."
            )
        if self.broker_writes_enabled:
            raise DisabledWriteTransportSafetyError(
                "Broker writes must remain disabled."
            )
        if self.submission_states_enabled:
            raise DisabledWriteTransportSafetyError(
                "Submitting and submitted states must remain unreachable."
            )

    @property
    def policy_digest(self) -> str:
        return sha256_document(self.to_document())

    def to_document(self) -> dict[str, object]:
        return {
            "base_url": self.base_url,
            "path": self.path,
            "method": self.method,
            "required_purpose": self.required_purpose.value,
            "maximum_envelope_ttl_seconds": self.maximum_envelope_ttl_seconds,
            "allow_redirects": False,
            "automatic_retries_enabled": False,
            "external_network_enabled": False,
            "broker_writes_enabled": False,
            "submission_states_enabled": False,
        }


@dataclass(frozen=True, slots=True)
class HumanApprovalGrant:
    challenge_id: str
    request_digest: str
    account_fingerprint: str
    approver_fingerprint: str
    granted_at: datetime
    expires_at: datetime
    scope: ApprovalScope = ApprovalScope.ORDER_CREATE_ONCE
    grant_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_nonblank(self.challenge_id, "Challenge ID")
        _require_sha256(self.request_digest, "Request digest")
        _require_sha256(self.account_fingerprint, "Account fingerprint")
        _require_sha256(self.approver_fingerprint, "Approver fingerprint")
        _require_aware(self.granted_at, "Approval grant time")
        _require_aware(self.expires_at, "Approval expiry")
        if self.expires_at <= self.granted_at:
            raise DisabledWriteTransportConfigurationError(
                "Approval expiry must follow grant time."
            )
        identity = {
            "challenge_id": self.challenge_id,
            "request_digest": self.request_digest,
            "account_fingerprint": self.account_fingerprint,
            "approver_fingerprint": self.approver_fingerprint,
            "granted_at": format_utc(self.granted_at),
            "expires_at": format_utc(self.expires_at),
            "scope": self.scope.value,
        }
        object.__setattr__(
            self,
            "grant_id",
            str(uuid5(_GRANT_NAMESPACE, sha256_document(identity))),
        )

    @classmethod
    def from_challenge(
        cls,
        challenge: HumanApprovalChallenge,
        *,
        approver_fingerprint: str,
        granted_at: datetime,
    ) -> HumanApprovalGrant:
        return cls(
            challenge_id=challenge.approval_id,
            request_digest=challenge.request_digest,
            account_fingerprint=challenge.account_fingerprint,
            approver_fingerprint=approver_fingerprint,
            granted_at=granted_at,
            expires_at=challenge.expires_at,
        )

    def to_document(self) -> dict[str, object]:
        return {
            "grant_id": self.grant_id,
            "challenge_id": self.challenge_id,
            "request_digest": self.request_digest,
            "account_fingerprint": self.account_fingerprint,
            "approver_fingerprint": self.approver_fingerprint,
            "granted_at": format_utc(self.granted_at),
            "expires_at": format_utc(self.expires_at),
            "scope": self.scope.value,
        }


@dataclass(frozen=True, slots=True)
class AuthorizedWriteEnvelope:
    compiled_request: CompiledTossOrderRequest
    approval_grant: HumanApprovalGrant
    token_lease: TokenLeaseMetadata
    token_use: TokenUseReceipt
    created_at: datetime
    expires_at: datetime
    state: TransportState = TransportState.CREDENTIAL_VALIDATED
    authorization_header: str = "Bearer [REDACTED]"
    envelope_id: str = field(init=False)
    envelope_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.compiled_request, CompiledTossOrderRequest):
            raise DisabledWriteTransportConfigurationError(
                "Compiled request evidence is invalid."
            )
        if not isinstance(self.approval_grant, HumanApprovalGrant):
            raise DisabledWriteTransportConfigurationError(
                "Approval grant evidence is invalid."
            )
        if not isinstance(self.token_lease, TokenLeaseMetadata):
            raise DisabledWriteTransportConfigurationError(
                "Token lease evidence is invalid."
            )
        if not isinstance(self.token_use, TokenUseReceipt):
            raise DisabledWriteTransportConfigurationError(
                "Token-use evidence is invalid."
            )
        _require_aware(self.created_at, "Envelope creation time")
        _require_aware(self.expires_at, "Envelope expiry")
        if self.expires_at <= self.created_at:
            raise DisabledWriteTransportConfigurationError(
                "Envelope expiry must follow creation."
            )
        if self.state is not TransportState.CREDENTIAL_VALIDATED:
            raise DisabledWriteTransportSafetyError(
                "Authorized envelope must stop at credential validation."
            )
        if self.authorization_header != "Bearer [REDACTED]":
            raise DisabledWriteTransportSafetyError(
                "Authorized envelope must not expose a raw token."
            )
        if self.compiled_request.network_sendable:
            raise DisabledWriteTransportSafetyError(
                "Compiled request cannot be network-sendable."
            )
        if self.compiled_request.credentials_loaded:
            raise DisabledWriteTransportSafetyError(
                "Compiled request cannot contain credentials."
            )
        if (
            self.approval_grant.request_digest
            != self.compiled_request.request_digest
        ):
            raise DisabledWriteTransportIntegrityError(
                "Approval request digest does not match compiled request."
            )
        if (
            self.approval_grant.account_fingerprint
            != self.compiled_request.account_fingerprint
        ):
            raise DisabledWriteTransportIntegrityError(
                "Approval account does not match compiled request."
            )
        if (
            self.token_lease.account_fingerprint
            != self.compiled_request.account_fingerprint
        ):
            raise DisabledWriteTransportIntegrityError(
                "Token lease account does not match compiled request."
            )
        if self.token_lease.purpose is not CredentialPurpose.ORDER_WRITE_DRY_RUN:
            raise DisabledWriteTransportSafetyError(
                "Token lease purpose is not valid for write-path integration."
            )
        if self.token_lease.state not in {
            TokenLeaseState.ACTIVE,
            TokenLeaseState.EXPIRING,
        }:
            raise DisabledWriteTransportSafetyError(
                f"Token lease is not active: {self.token_lease.state.value}."
            )
        if self.token_lease.use_count < 1:
            raise DisabledWriteTransportIntegrityError(
                "Token lease must record at least one authorized use."
            )
        if self.token_use.lease_id != self.token_lease.lease_id:
            raise DisabledWriteTransportIntegrityError(
                "Token-use receipt lease ID does not match."
            )
        if self.token_use.token_fingerprint != self.token_lease.token_fingerprint:
            raise DisabledWriteTransportIntegrityError(
                "Token-use receipt fingerprint does not match."
            )
        if self.token_use.purpose is not self.token_lease.purpose:
            raise DisabledWriteTransportIntegrityError(
                "Token-use purpose does not match lease purpose."
            )
        if self.token_use.authorization_header != "Bearer [REDACTED]":
            raise DisabledWriteTransportSafetyError(
                "Token-use receipt must remain redacted."
            )
        identity = self._identity_document()
        digest = sha256_document(identity)
        object.__setattr__(self, "envelope_digest", digest)
        object.__setattr__(
            self,
            "envelope_id",
            str(uuid5(_ENVELOPE_NAMESPACE, digest)),
        )

    def _identity_document(self) -> dict[str, object]:
        return {
            "compiled_request": self.compiled_request.to_document(),
            "approval_grant": self.approval_grant.to_document(),
            "token_lease": self.token_lease.to_document(),
            "token_use": self.token_use.to_document(),
            "created_at": format_utc(self.created_at),
            "expires_at": format_utc(self.expires_at),
            "state": self.state.value,
            "authorization_header": self.authorization_header,
        }

    def to_document(self) -> dict[str, object]:
        return {
            "envelope_id": self.envelope_id,
            **self._identity_document(),
            "envelope_digest": self.envelope_digest,
        }


@dataclass(frozen=True, slots=True)
class TransportBlockReceipt:
    envelope_digest: str
    request_digest: str
    evaluated_at: datetime
    state: TransportState = TransportState.TRANSPORT_BLOCKED
    outcome: TransportOutcome = TransportOutcome.WRITE_TRANSPORT_DISABLED
    retry_disposition: RetryDisposition = RetryDisposition.NO_AUTOMATIC_RETRY
    reason: str = "External broker write transport is disabled."
    network_call_count: int = 0
    broker_write_count: int = 0
    redirect_count: int = 0
    attempt_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.envelope_digest, "Envelope digest")
        _require_sha256(self.request_digest, "Request digest")
        _require_aware(self.evaluated_at, "Transport evaluation time")
        if self.state is not TransportState.TRANSPORT_BLOCKED:
            raise DisabledWriteTransportSafetyError(
                "Disabled transport must finish in TRANSPORT_BLOCKED."
            )
        if self.outcome is not TransportOutcome.WRITE_TRANSPORT_DISABLED:
            raise DisabledWriteTransportSafetyError(
                "Disabled transport outcome cannot indicate submission."
            )
        if self.retry_disposition is not RetryDisposition.NO_AUTOMATIC_RETRY:
            raise DisabledWriteTransportSafetyError(
                "Automatic order retries must remain disabled."
            )
        _require_nonblank(self.reason, "Transport block reason")
        for count, name in (
            (self.network_call_count, "Network call count"),
            (self.broker_write_count, "Broker write count"),
            (self.redirect_count, "Redirect count"),
        ):
            if count != 0:
                raise DisabledWriteTransportSafetyError(
                    f"{name} must remain zero."
                )
        identity = {
            "envelope_digest": self.envelope_digest,
            "request_digest": self.request_digest,
            "evaluated_at": format_utc(self.evaluated_at),
            "state": self.state.value,
            "outcome": self.outcome.value,
            "retry_disposition": self.retry_disposition.value,
            "reason": self.reason,
            "network_call_count": 0,
            "broker_write_count": 0,
            "redirect_count": 0,
        }
        object.__setattr__(
            self,
            "attempt_id",
            str(uuid5(_ATTEMPT_NAMESPACE, sha256_document(identity))),
        )

    def to_document(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "envelope_digest": self.envelope_digest,
            "request_digest": self.request_digest,
            "evaluated_at": format_utc(self.evaluated_at),
            "state": self.state.value,
            "outcome": self.outcome.value,
            "retry_disposition": self.retry_disposition.value,
            "reason": self.reason,
            "network_call_count": 0,
            "broker_write_count": 0,
            "redirect_count": 0,
        }


@dataclass(frozen=True, slots=True)
class TransportIntegrationCheck:
    name: str
    passed: bool
    detail: str

    def __post_init__(self) -> None:
        _require_nonblank(self.name, "Check name")
        if not isinstance(self.passed, bool):
            raise DisabledWriteTransportConfigurationError(
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
class DisabledWriteTransportReport:
    evaluated_at: datetime
    policy: DisabledWriteTransportPolicy
    dry_run_report_digest: str
    challenge: HumanApprovalChallenge
    checks: tuple[TransportIntegrationCheck, ...]
    decision: IntegrationDecision
    envelope: AuthorizedWriteEnvelope | None
    receipt: TransportBlockReceipt | None
    schema_version: int = _SCHEMA_VERSION
    report_id: str = field(init=False)
    report_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _require_aware(self.evaluated_at, "Report evaluation time")
        if not isinstance(self.policy, DisabledWriteTransportPolicy):
            raise DisabledWriteTransportConfigurationError(
                "Transport policy is invalid."
            )
        _require_sha256(self.dry_run_report_digest, "Dry-run report digest")
        if not isinstance(self.challenge, HumanApprovalChallenge):
            raise DisabledWriteTransportConfigurationError(
                "Approval challenge is invalid."
            )
        if not self.checks:
            raise DisabledWriteTransportIntegrityError(
                "Transport integration report requires checks."
            )
        failed_checks = tuple(check for check in self.checks if not check.passed)
        if self.decision is IntegrationDecision.CERTIFIED_BLOCKED:
            if failed_checks:
                raise DisabledWriteTransportIntegrityError(
                    "Certified reports cannot contain failed checks."
                )
            if self.envelope is None or self.receipt is None:
                raise DisabledWriteTransportIntegrityError(
                    "Certified reports require envelope and block receipt."
                )
            if self.receipt.envelope_digest != self.envelope.envelope_digest:
                raise DisabledWriteTransportIntegrityError(
                    "Transport receipt does not match the envelope."
                )
            if (
                self.receipt.request_digest
                != self.envelope.compiled_request.request_digest
            ):
                raise DisabledWriteTransportIntegrityError(
                    "Transport receipt does not match the request."
                )
        elif self.decision is IntegrationDecision.REJECTED:
            if not failed_checks:
                raise DisabledWriteTransportIntegrityError(
                    "Rejected reports require at least one failed check."
                )
            if self.envelope is not None or self.receipt is not None:
                raise DisabledWriteTransportIntegrityError(
                    "Rejected reports cannot create transport evidence."
                )
        else:
            raise DisabledWriteTransportConfigurationError(
                "Transport integration decision is invalid."
            )
        identity = self._identity_document()
        digest = sha256_document(identity)
        object.__setattr__(self, "report_digest", digest)
        object.__setattr__(
            self,
            "report_id",
            str(uuid5(_REPORT_NAMESPACE, digest)),
        )

    @property
    def broker_write_count(self) -> int:
        return 0 if self.receipt is None else self.receipt.broker_write_count

    @property
    def network_call_count(self) -> int:
        return 0 if self.receipt is None else self.receipt.network_call_count

    def _identity_document(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "evaluated_at": format_utc(self.evaluated_at),
            "policy": self.policy.to_document(),
            "dry_run_report_digest": self.dry_run_report_digest,
            "challenge": self.challenge.to_document(),
            "checks": [check.to_document() for check in self.checks],
            "decision": self.decision.value,
            "envelope": (
                None if self.envelope is None else self.envelope.to_document()
            ),
            "receipt": (
                None if self.receipt is None else self.receipt.to_document()
            ),
            "external_network_enabled": False,
            "broker_writes_enabled": False,
            "submission_states_enabled": False,
        }

    def to_document(self) -> dict[str, object]:
        return {
            "report_id": self.report_id,
            **self._identity_document(),
            "report_digest": self.report_digest,
            "network_call_count": self.network_call_count,
            "broker_write_count": self.broker_write_count,
        }


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def sha256_document(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def format_utc(value: datetime) -> str:
    _require_aware(value, "Timestamp")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _require_nonblank(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise DisabledWriteTransportConfigurationError(
            f"{name} must be nonblank text."
        )


def _require_sha256(value: str, name: str) -> None:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise DisabledWriteTransportConfigurationError(
            f"{name} must be a lowercase SHA-256 digest."
        )


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DisabledWriteTransportConfigurationError(
            f"{name} must be timezone-aware."
        )


def _require_positive_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DisabledWriteTransportConfigurationError(
            f"{name} must be a positive integer."
        )
