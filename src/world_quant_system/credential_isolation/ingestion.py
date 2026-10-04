from __future__ import annotations

import json
from pathlib import Path

from world_quant_system.credential_isolation.models import (
    CredentialIsolationConfigurationError,
    CredentialIsolationPolicy,
    CredentialPurpose,
    SecretSource,
)
from world_quant_system.credential_isolation.providers import FakeSecretProvider


def load_policy(path: str | Path) -> CredentialIsolationPolicy:
    document = _load_object(path)
    return CredentialIsolationPolicy(
        provider=_text(document, "provider"),
        permitted_sources=tuple(
            SecretSource(value)
            for value in _string_list(document, "permitted_sources")
        ),
        permitted_purposes=tuple(
            CredentialPurpose(value)
            for value in _string_list(document, "permitted_purposes")
        ),
        maximum_token_ttl_seconds=_integer(
            document,
            "maximum_token_ttl_seconds",
        ),
        expiring_window_seconds=_integer(
            document,
            "expiring_window_seconds",
        ),
    )


def load_fixture_provider(path: str | Path) -> FakeSecretProvider:
    document = _load_object(path)
    return FakeSecretProvider(
        client_id=_text(document, "client_id"),
        client_secret=_text(document, "client_secret"),
        account_id=_text(document, "account_id"),
        credential_version=_text(document, "credential_version"),
    )


def _load_object(path: str | Path) -> dict[str, object]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CredentialIsolationConfigurationError(
            f"Unable to load JSON document: {path}"
        ) from error
    if not isinstance(value, dict):
        raise CredentialIsolationConfigurationError(
            "Credential JSON root must be an object."
        )
    return value


def _text(document: dict[str, object], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CredentialIsolationConfigurationError(
            f"{key} must be nonblank text."
        )
    return value


def _integer(document: dict[str, object], key: str) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CredentialIsolationConfigurationError(
            f"{key} must be an integer."
        )
    return value


def _string_list(document: dict[str, object], key: str) -> list[str]:
    value = document.get(key)
    if not isinstance(value, list) or not value:
        raise CredentialIsolationConfigurationError(
            f"{key} must be a nonempty list."
        )
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise CredentialIsolationConfigurationError(
            f"{key} must contain nonblank text values."
        )
    return [item for item in value if isinstance(item, str)]
