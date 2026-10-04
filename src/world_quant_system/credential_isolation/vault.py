from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid5

from world_quant_system.credential_isolation.models import (
    CredentialIsolationConfigurationError,
    CredentialIsolationIntegrityError,
    CredentialIsolationSafetyError,
    CredentialPurpose,
    TokenLeaseMetadata,
    TokenLeaseState,
    TokenUseReceipt,
    sha256_secret,
)
from world_quant_system.credential_isolation.providers import CredentialMaterial
from world_quant_system.paper_execution.models import (
    KillSwitchMode,
    KillSwitchState,
)

_LEASE_NAMESPACE = UUID("4049b06b-d78c-529d-b68c-b5c6bc4d5fe3")


@dataclass(frozen=True, slots=True)
class IssuedToken:
    secret: str
    token_type: str
    expires_in_seconds: int

    def __post_init__(self) -> None:
        if not self.secret.strip():
            raise CredentialIsolationConfigurationError(
                "Issued token secret cannot be blank."
            )
        if self.token_type.casefold() != "bearer":
            raise CredentialIsolationConfigurationError(
                "Only Bearer tokens are supported."
            )
        if self.expires_in_seconds <= 0:
            raise CredentialIsolationConfigurationError(
                "Token expiration must be positive."
            )


class TokenIssuer(Protocol):
    def issue(
        self,
        *,
        client_id: bytes,
        client_secret: bytes,
        purpose: CredentialPurpose,
    ) -> IssuedToken:
        """Issue a token without retaining source credentials."""
        ...


class NoNetworkTokenIssuer:
    def issue(
        self,
        *,
        client_id: bytes,
        client_secret: bytes,
        purpose: CredentialPurpose,
    ) -> IssuedToken:
        del client_id, client_secret, purpose
        raise CredentialIsolationSafetyError(
            "Network token issuance is disabled in credential isolation v1."
        )


class SyntheticNoNetworkTokenIssuer:
    """Generate a local test token without external I/O."""

    def __init__(self, *, ttl_seconds: int = 3_600) -> None:
        if ttl_seconds <= 0:
            raise CredentialIsolationConfigurationError(
                "Synthetic token TTL must be positive."
            )
        self._ttl_seconds = ttl_seconds
        self.issue_count = 0

    def issue(
        self,
        *,
        client_id: bytes,
        client_secret: bytes,
        purpose: CredentialPurpose,
    ) -> IssuedToken:
        self.issue_count += 1
        digest = hashlib.sha256(
            b"wqs-synthetic-token-v1\0"
            + client_id
            + b"\0"
            + client_secret
            + b"\0"
            + purpose.value.encode()
        ).hexdigest()
        return IssuedToken(
            secret=f"synthetic.{digest}",
            token_type="Bearer",
            expires_in_seconds=self._ttl_seconds,
        )


@dataclass(slots=True)
class _VaultEntry:
    secret: bytearray
    metadata: TokenLeaseMetadata

    def destroy(self) -> None:
        for index in range(len(self.secret)):
            self.secret[index] = 0
        self.secret.clear()
        self.metadata = replace(
            self.metadata,
            state=TokenLeaseState.DESTROYED,
        )


class EphemeralTokenVault:
    def __init__(
        self,
        *,
        maximum_ttl_seconds: int,
        expiring_window_seconds: int,
    ) -> None:
        if maximum_ttl_seconds <= 0:
            raise CredentialIsolationConfigurationError(
                "Maximum token TTL must be positive."
            )
        if expiring_window_seconds < 0:
            raise CredentialIsolationConfigurationError(
                "Expiring window cannot be negative."
            )
        if expiring_window_seconds >= maximum_ttl_seconds:
            raise CredentialIsolationConfigurationError(
                "Expiring window must be shorter than maximum token TTL."
            )
        self._maximum_ttl_seconds = maximum_ttl_seconds
        self._expiring_window_seconds = expiring_window_seconds
        self._entries: dict[str, _VaultEntry] = {}

    def issue(
        self,
        *,
        material: CredentialMaterial,
        issuer: TokenIssuer,
        purpose: CredentialPurpose,
        account_fingerprint: str,
        client_id_fingerprint: str,
        now: datetime,
        kill_switch: KillSwitchState,
    ) -> TokenLeaseMetadata:
        _require_aware(now)
        _require_normal_kill_switch(kill_switch)
        with (
            material.client_id.reveal_bytes() as client_id,
            material.client_secret.reveal_bytes() as client_secret,
        ):
            issued = issuer.issue(
                client_id=client_id,
                client_secret=client_secret,
                purpose=purpose,
            )
        ttl_seconds = min(
            issued.expires_in_seconds,
            self._maximum_ttl_seconds,
        )
        expires_at = now.astimezone(UTC) + timedelta(seconds=ttl_seconds)
        token_bytes = issued.secret.encode()
        token_fingerprint = sha256_secret(token_bytes)
        identity = "|".join(
            (
                account_fingerprint,
                client_id_fingerprint,
                purpose.value,
                now.astimezone(UTC).isoformat(),
                token_fingerprint,
            )
        )
        lease_id = str(uuid5(_LEASE_NAMESPACE, identity))
        if lease_id in self._entries:
            raise CredentialIsolationIntegrityError(
                "Duplicate token lease identity detected."
            )
        metadata = TokenLeaseMetadata(
            lease_id=lease_id,
            token_fingerprint=token_fingerprint,
            account_fingerprint=account_fingerprint,
            client_id_fingerprint=client_id_fingerprint,
            purpose=purpose,
            issued_at=now.astimezone(UTC),
            expires_at=expires_at,
            state=TokenLeaseState.ISSUED,
            use_count=0,
        )
        self._entries[lease_id] = _VaultEntry(
            secret=bytearray(token_bytes),
            metadata=metadata,
        )
        return metadata

    def authorize(
        self,
        *,
        lease_id: str,
        purpose: CredentialPurpose,
        account_fingerprint: str,
        now: datetime,
        kill_switch: KillSwitchState,
    ) -> TokenUseReceipt:
        _require_aware(now)
        _require_normal_kill_switch(kill_switch)
        entry = self._entry(lease_id)
        metadata = self._state_at(entry.metadata, now)
        if metadata.state in {
            TokenLeaseState.EXPIRED,
            TokenLeaseState.REVOKED,
            TokenLeaseState.DESTROYED,
        }:
            raise CredentialIsolationSafetyError(
                f"Token lease is not usable: {metadata.state.value}."
            )
        if metadata.purpose is not purpose:
            raise CredentialIsolationSafetyError(
                "Token lease purpose does not match the request."
            )
        if metadata.account_fingerprint != account_fingerprint:
            raise CredentialIsolationSafetyError(
                "Token lease account fingerprint does not match."
            )
        updated = replace(
            metadata,
            state=(
                TokenLeaseState.EXPIRING
                if metadata.state is TokenLeaseState.EXPIRING
                else TokenLeaseState.ACTIVE
            ),
            use_count=metadata.use_count + 1,
        )
        entry.metadata = updated
        return TokenUseReceipt(
            lease_id=lease_id,
            token_fingerprint=updated.token_fingerprint,
            purpose=purpose,
            used_at=now.astimezone(UTC),
            authorization_header="Bearer [REDACTED]",
        )

    def revoke(self, lease_id: str) -> TokenLeaseMetadata:
        entry = self._entry(lease_id)
        if entry.metadata.state is TokenLeaseState.DESTROYED:
            raise CredentialIsolationIntegrityError(
                "Destroyed token lease cannot be revoked."
            )
        entry.metadata = replace(
            entry.metadata,
            state=TokenLeaseState.REVOKED,
        )
        return entry.metadata

    def destroy(self, lease_id: str) -> TokenLeaseMetadata:
        entry = self._entry(lease_id)
        entry.destroy()
        return entry.metadata

    def metadata(self, lease_id: str, *, now: datetime) -> TokenLeaseMetadata:
        entry = self._entry(lease_id)
        entry.metadata = self._state_at(entry.metadata, now)
        return entry.metadata

    def secret_destroyed(self, lease_id: str) -> bool:
        return len(self._entry(lease_id).secret) == 0

    def _entry(self, lease_id: str) -> _VaultEntry:
        try:
            return self._entries[lease_id]
        except KeyError as error:
            raise CredentialIsolationConfigurationError(
                "Unknown token lease ID."
            ) from error

    def _state_at(
        self,
        metadata: TokenLeaseMetadata,
        now: datetime,
    ) -> TokenLeaseMetadata:
        _require_aware(now)
        if metadata.state in {
            TokenLeaseState.REVOKED,
            TokenLeaseState.DESTROYED,
        }:
            return metadata
        normalized_now = now.astimezone(UTC)
        if normalized_now >= metadata.expires_at:
            return replace(metadata, state=TokenLeaseState.EXPIRED)
        expiring_at = metadata.expires_at - timedelta(
            seconds=self._expiring_window_seconds
        )
        if normalized_now >= expiring_at:
            return replace(metadata, state=TokenLeaseState.EXPIRING)
        if metadata.use_count > 0:
            return replace(metadata, state=TokenLeaseState.ACTIVE)
        return metadata


def _require_normal_kill_switch(kill_switch: KillSwitchState) -> None:
    if kill_switch.mode is not KillSwitchMode.NORMAL:
        raise CredentialIsolationSafetyError(
            "Token use requires the kill switch to be NORMAL."
        )


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CredentialIsolationConfigurationError(
            "Token timestamp must be timezone-aware."
        )
