from __future__ import annotations

import re
from collections.abc import Iterable

from world_quant_system.credential_isolation.models import (
    CredentialIsolationSafetyError,
)

_BEARER_PATTERN = re.compile(
    r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b"
)
_KEY_VALUE_PATTERN = re.compile(
    r"(?i)(client_secret|access_token|refresh_token|account_id)"
    r"\s*[:=]\s*[^\s,;}]+"
)


class SecretRedactor:
    def __init__(self, secrets: Iterable[bytes]) -> None:
        unique = {secret for secret in secrets if secret}
        self._secrets = [
            bytearray(secret)
            for secret in sorted(unique, key=len, reverse=True)
        ]
        self._destroyed = False

    @property
    def destroyed(self) -> bool:
        return self._destroyed

    def redact_text(self, value: str) -> str:
        self._require_active()
        redacted = value
        for buffer in self._secrets:
            redacted = redacted.replace(
                bytes(buffer).decode(errors="strict"),
                "[REDACTED]",
            )
        redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", redacted)
        return _KEY_VALUE_PATTERN.sub(r"\1=[REDACTED]", redacted)

    def assert_safe_bytes(self, payload: bytes) -> None:
        self._require_active()
        for buffer in self._secrets:
            if bytes(buffer) in payload:
                raise CredentialIsolationSafetyError(
                    "Serialized payload contains credential material."
                )
        text = payload.decode(errors="replace")
        if _BEARER_PATTERN.search(text) is not None:
            raise CredentialIsolationSafetyError(
                "Serialized payload contains an unredacted Bearer token."
            )

    def destroy(self) -> None:
        for buffer in self._secrets:
            for index in range(len(buffer)):
                buffer[index] = 0
            buffer.clear()
        self._secrets.clear()
        self._destroyed = True

    def _require_active(self) -> None:
        if self._destroyed:
            raise CredentialIsolationSafetyError(
                "Redaction buffer has already been destroyed."
            )
