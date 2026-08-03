from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

from world_quant_system.credential_isolation.models import (
    CredentialIsolationConfigurationError,
    SecretSource,
)

_DEFAULT_ENVIRONMENT_NAMES = {
    "client_id": "WQS_TOSS_CLIENT_ID",
    "client_secret": "WQS_TOSS_CLIENT_SECRET",
    "account_id": "WQS_TOSS_ACCOUNT_ID",
    "credential_version": "WQS_TOSS_CREDENTIAL_VERSION",
}


class SecretValue:
    """Mutable secret buffer that can be explicitly zeroized."""

    def __init__(self, value: str, *, field_name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise CredentialIsolationConfigurationError(
                f"{field_name} cannot be blank."
            )
        self._buffer = bytearray(value.encode())
        self._destroyed = False
        self._field_name = field_name

    @property
    def destroyed(self) -> bool:
        return self._destroyed

    @contextmanager
    def reveal_bytes(self) -> Iterator[bytes]:
        if self._destroyed:
            raise CredentialIsolationConfigurationError(
                f"{self._field_name} has already been destroyed."
            )
        temporary = bytes(self._buffer)
        try:
            yield temporary
        finally:
            temporary = b""

    def destroy(self) -> None:
        for index in range(len(self._buffer)):
            self._buffer[index] = 0
        self._buffer.clear()
        self._destroyed = True

    def __repr__(self) -> str:
        return "SecretValue([REDACTED])"


@dataclass(slots=True)
class CredentialMaterial:
    provider: str
    source: SecretSource
    client_id: SecretValue
    client_secret: SecretValue
    account_id: SecretValue
    credential_version: SecretValue

    @property
    def destroyed(self) -> bool:
        return all(
            value.destroyed
            for value in (
                self.client_id,
                self.client_secret,
                self.account_id,
                self.credential_version,
            )
        )

    def destroy(self) -> None:
        self.client_id.destroy()
        self.client_secret.destroy()
        self.account_id.destroy()
        self.credential_version.destroy()

    def __enter__(self) -> CredentialMaterial:
        return self

    def __exit__(self, *_: object) -> None:
        self.destroy()

    def __repr__(self) -> str:
        return (
            "CredentialMaterial(provider="
            f"{self.provider!r}, source={self.source.value!r}, "
            "secrets=[REDACTED])"
        )


class SecretProvider(Protocol):
    source: SecretSource

    def load(self, provider: str) -> CredentialMaterial:
        """Load one ephemeral credential bundle."""
        ...


class EnvironmentSecretProvider:
    source = SecretSource.ENVIRONMENT

    def __init__(
        self,
        environment: Mapping[str, str] | None = None,
        *,
        variable_names: Mapping[str, str] | None = None,
    ) -> None:
        self._environment = os.environ if environment is None else environment
        self._variable_names = dict(
            _DEFAULT_ENVIRONMENT_NAMES
            if variable_names is None
            else variable_names
        )
        required_keys = set(_DEFAULT_ENVIRONMENT_NAMES)
        if set(self._variable_names) != required_keys:
            raise CredentialIsolationConfigurationError(
                "Environment secret variable names are incomplete."
            )

    def load(self, provider: str) -> CredentialMaterial:
        values: dict[str, str] = {}
        missing: list[str] = []
        for field_name, variable_name in self._variable_names.items():
            value = self._environment.get(variable_name)
            if value is None or not value.strip():
                missing.append(variable_name)
            else:
                values[field_name] = value
        if missing:
            raise CredentialIsolationConfigurationError(
                "Required credential environment variables are missing: "
                + ", ".join(sorted(missing))
            )
        return _build_material(provider, self.source, values)


class KeychainReader(Protocol):
    def read_secret(self, *, service: str, account: str) -> str:
        """Read one secret through an injected keychain boundary."""
        ...


class MacOSKeychainSecretProvider:
    """Dependency-injected keychain adapter; performs no subprocess call."""

    source = SecretSource.MACOS_KEYCHAIN

    def __init__(
        self,
        reader: KeychainReader,
        *,
        service: str = "world-quant-system.toss",
    ) -> None:
        if not service.strip():
            raise CredentialIsolationConfigurationError(
                "Keychain service cannot be blank."
            )
        self._reader = reader
        self._service = service

    def load(self, provider: str) -> CredentialMaterial:
        values = {
            field_name: self._reader.read_secret(
                service=self._service,
                account=field_name,
            )
            for field_name in _DEFAULT_ENVIRONMENT_NAMES
        }
        return _build_material(provider, self.source, values)


class FakeSecretProvider:
    source = SecretSource.FIXTURE

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        account_id: str,
        credential_version: str,
    ) -> None:
        self._values = {
            "client_id": client_id,
            "client_secret": client_secret,
            "account_id": account_id,
            "credential_version": credential_version,
        }

    def load(self, provider: str) -> CredentialMaterial:
        return _build_material(provider, self.source, self._values)


def _build_material(
    provider: str,
    source: SecretSource,
    values: Mapping[str, str],
) -> CredentialMaterial:
    return CredentialMaterial(
        provider=provider,
        source=source,
        client_id=SecretValue(values["client_id"], field_name="Client ID"),
        client_secret=SecretValue(
            values["client_secret"],
            field_name="Client secret",
        ),
        account_id=SecretValue(values["account_id"], field_name="Account ID"),
        credential_version=SecretValue(
            values["credential_version"],
            field_name="Credential version",
        ),
    )
