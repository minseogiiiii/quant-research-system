from datetime import UTC, datetime

import pytest

from world_quant_system.credential_isolation.models import (
    CredentialFingerprint,
    CredentialIsolationConfigurationError,
    CredentialIsolationPolicy,
    CredentialIsolationSafetyError,
    SecretSource,
)
from world_quant_system.credential_isolation.providers import (
    EnvironmentSecretProvider,
    FakeSecretProvider,
    MacOSKeychainSecretProvider,
    SecretValue,
)


def test_secret_value_repr_and_destroy() -> None:
    value = SecretValue("super-secret", field_name="test")

    assert "super-secret" not in repr(value)
    with value.reveal_bytes() as revealed:
        assert revealed == b"super-secret"
    value.destroy()

    assert value.destroyed
    with pytest.raises(CredentialIsolationConfigurationError), value.reveal_bytes():
        pass


def test_fake_material_zeroizes_all_fields() -> None:
    material = FakeSecretProvider(
        client_id="client",
        client_secret="secret",
        account_id="account",
        credential_version="v1",
    ).load("toss")

    destroyed_before = material.destroyed
    assert destroyed_before is False

    material.destroy()
    assert material.destroyed
    rendered = repr(material)
    assert "client" not in rendered
    assert "account" not in rendered


def test_environment_provider_requires_fixed_variables() -> None:
    provider = EnvironmentSecretProvider(environment={})

    with pytest.raises(
        CredentialIsolationConfigurationError,
        match="WQS_TOSS_ACCOUNT_ID",
    ):
        provider.load("toss")


def test_environment_provider_loads_without_mutating_environment() -> None:
    environment = {
        "WQS_TOSS_CLIENT_ID": "client",
        "WQS_TOSS_CLIENT_SECRET": "secret",
        "WQS_TOSS_ACCOUNT_ID": "account",
        "WQS_TOSS_CREDENTIAL_VERSION": "v1",
    }
    material = EnvironmentSecretProvider(environment=environment).load("toss")

    assert material.source is SecretSource.ENVIRONMENT
    material.destroy()
    assert environment["WQS_TOSS_CLIENT_SECRET"] == "secret"


class _FakeKeychainReader:
    def read_secret(self, *, service: str, account: str) -> str:
        assert service == "world-quant-system.toss"
        return f"keychain-{account}"


def test_keychain_provider_uses_injected_reader_only() -> None:
    material = MacOSKeychainSecretProvider(_FakeKeychainReader()).load("toss")

    assert material.source is SecretSource.MACOS_KEYCHAIN
    material.destroy()
    assert material.destroyed


def test_policy_rejects_secret_persistence() -> None:
    with pytest.raises(CredentialIsolationSafetyError, match="persistence"):
        CredentialIsolationPolicy(secret_persistence_enabled=True)


def test_fingerprint_timestamp_requires_timezone() -> None:
    with pytest.raises(CredentialIsolationConfigurationError):
        CredentialFingerprint(
            provider="toss",
            source=SecretSource.FIXTURE,
            account_fingerprint="a" * 64,
            client_id_fingerprint="b" * 64,
            credential_version_fingerprint="c" * 64,
            loaded_at=datetime.now(),
        )

    valid = CredentialFingerprint(
        provider="toss",
        source=SecretSource.FIXTURE,
        account_fingerprint="a" * 64,
        client_id_fingerprint="b" * 64,
        credential_version_fingerprint="c" * 64,
        loaded_at=datetime.now(UTC),
    )
    assert valid.account_fingerprint == "a" * 64
