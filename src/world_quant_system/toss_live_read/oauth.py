from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote_from_bytes

from world_quant_system.credential_isolation.providers import CredentialMaterial
from world_quant_system.toss_live_read.http_transport import TossTransport
from world_quant_system.toss_live_read.models import (
    OFFICIAL_TOKEN_PATH,
    TossLiveReadIntegrityError,
    TossLiveReadSafetyError,
    TossLiveReadTransportError,
    sha256_secret,
)


@dataclass(slots=True)
class EphemeralOAuthToken:
    _secret: bytearray
    token_fingerprint: str
    issued_at: datetime
    expires_at: datetime
    _destroyed: bool = False

    @property
    def destroyed(self) -> bool:
        return self._destroyed

    @property
    def secret_destroyed(self) -> bool:
        return not self._secret

    @contextmanager
    def authorization_header(self) -> Iterator[str]:
        if self._destroyed:
            raise TossLiveReadSafetyError("OAuth token has been destroyed.")
        temporary = self._secret.decode("utf-8")
        try:
            yield f"Bearer {temporary}"
        finally:
            temporary = ""

    def destroy(self) -> None:
        for index in range(len(self._secret)):
            self._secret[index] = 0
        self._secret.clear()
        self._destroyed = True

    def __enter__(self) -> EphemeralOAuthToken:
        return self

    def __exit__(self, *_: object) -> None:
        self.destroy()


class TossOAuthClient:
    def __init__(self, transport: TossTransport) -> None:
        self._transport = transport

    def issue(
        self,
        *,
        material: CredentialMaterial,
        now: datetime,
    ) -> EphemeralOAuthToken:
        if now.tzinfo is None or now.utcoffset() is None:
            raise TossLiveReadSafetyError(
                "OAuth issuance timestamp must be timezone-aware."
            )
        with (
            material.client_id.reveal_bytes() as client_id,
            material.client_secret.reveal_bytes() as client_secret,
        ):
            body = b"&".join(
                (
                    b"grant_type=client_credentials",
                    b"client_id=" + quote_from_bytes(client_id).encode(),
                    b"client_secret=" + quote_from_bytes(client_secret).encode(),
                )
            )
        response = self._transport.request(
            method="POST",
            path=OFFICIAL_TOKEN_PATH,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "world-quant-system/read-only-certification-v1",
            },
            body=body,
        )
        body = b""
        if response.status_code != 200:
            raise TossLiveReadTransportError(
                f"OAuth token issuance failed with HTTP {response.status_code}."
            )
        document = response.json_body
        if not isinstance(document, dict):
            raise TossLiveReadIntegrityError("OAuth response must be an object.")
        access_token = document.get("access_token")
        token_type = document.get("token_type")
        expires_in = document.get("expires_in")
        if not isinstance(access_token, str) or not access_token.strip():
            raise TossLiveReadIntegrityError("OAuth access_token is missing.")
        if not isinstance(token_type, str) or token_type.casefold() != "bearer":
            raise TossLiveReadIntegrityError("OAuth token_type must be Bearer.")
        if isinstance(expires_in, bool) or not isinstance(expires_in, int):
            raise TossLiveReadIntegrityError("OAuth expires_in must be an integer.")
        if not 1 <= expires_in <= 86_400:
            raise TossLiveReadIntegrityError("OAuth expires_in is outside policy.")
        token_bytes = access_token.encode()
        token = EphemeralOAuthToken(
            _secret=bytearray(token_bytes),
            token_fingerprint=sha256_secret(token_bytes),
            issued_at=now.astimezone(UTC),
            expires_at=now.astimezone(UTC) + timedelta(seconds=expires_in),
        )
        access_token = ""
        token_bytes = b""
        return token
