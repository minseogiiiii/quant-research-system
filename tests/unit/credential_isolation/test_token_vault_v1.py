from datetime import UTC, datetime, timedelta

import pytest

from world_quant_system.credential_isolation.models import (
    CredentialIsolationSafetyError,
    CredentialPurpose,
    TokenLeaseState,
    sha256_secret,
)
from world_quant_system.credential_isolation.providers import (
    CredentialMaterial,
    FakeSecretProvider,
)
from world_quant_system.credential_isolation.vault import (
    EphemeralTokenVault,
    SyntheticNoNetworkTokenIssuer,
)
from world_quant_system.paper_execution.models import (
    KillSwitchMode,
    KillSwitchState,
)

NOW = datetime(2026, 8, 3, 22, 30, tzinfo=UTC)
NORMAL = KillSwitchState(
    mode=KillSwitchMode.NORMAL,
    reason="normal",
    activated_at=None,
)


def _material() -> CredentialMaterial:
    return FakeSecretProvider(
        client_id="client",
        client_secret="secret",
        account_id="account",
        credential_version="v1",
    ).load("toss")


def _fingerprints(material: CredentialMaterial) -> tuple[str, str]:
    with material.account_id.reveal_bytes() as account:
        account_fingerprint = sha256_secret(account)
    with material.client_id.reveal_bytes() as client_id:
        client_fingerprint = sha256_secret(client_id)
    return account_fingerprint, client_fingerprint


def test_issue_authorize_and_destroy() -> None:
    material = _material()
    account, client = _fingerprints(material)
    vault = EphemeralTokenVault(
        maximum_ttl_seconds=3_600,
        expiring_window_seconds=60,
    )
    issuer = SyntheticNoNetworkTokenIssuer(ttl_seconds=600)

    lease = vault.issue(
        material=material,
        issuer=issuer,
        purpose=CredentialPurpose.ORDER_WRITE_DRY_RUN,
        account_fingerprint=account,
        client_id_fingerprint=client,
        now=NOW,
        kill_switch=NORMAL,
    )
    receipt = vault.authorize(
        lease_id=lease.lease_id,
        purpose=CredentialPurpose.ORDER_WRITE_DRY_RUN,
        account_fingerprint=account,
        now=NOW,
        kill_switch=NORMAL,
    )

    assert issuer.issue_count == 1
    assert receipt.authorization_header == "Bearer [REDACTED]"
    assert vault.metadata(lease.lease_id, now=NOW).use_count == 1
    destroyed = vault.destroy(lease.lease_id)
    assert destroyed.state is TokenLeaseState.DESTROYED
    assert vault.secret_destroyed(lease.lease_id)
    material.destroy()


def test_wrong_purpose_and_account_are_rejected() -> None:
    material = _material()
    account, client = _fingerprints(material)
    vault = EphemeralTokenVault(
        maximum_ttl_seconds=3_600,
        expiring_window_seconds=60,
    )
    lease = vault.issue(
        material=material,
        issuer=SyntheticNoNetworkTokenIssuer(),
        purpose=CredentialPurpose.READ_ONLY_PREPARATION,
        account_fingerprint=account,
        client_id_fingerprint=client,
        now=NOW,
        kill_switch=NORMAL,
    )

    with pytest.raises(CredentialIsolationSafetyError, match="purpose"):
        vault.authorize(
            lease_id=lease.lease_id,
            purpose=CredentialPurpose.ORDER_WRITE_DRY_RUN,
            account_fingerprint=account,
            now=NOW,
            kill_switch=NORMAL,
        )
    with pytest.raises(CredentialIsolationSafetyError, match="account"):
        vault.authorize(
            lease_id=lease.lease_id,
            purpose=CredentialPurpose.READ_ONLY_PREPARATION,
            account_fingerprint="f" * 64,
            now=NOW,
            kill_switch=NORMAL,
        )
    vault.destroy(lease.lease_id)
    material.destroy()


def test_expired_revoked_and_halted_leases_are_unusable() -> None:
    material = _material()
    account, client = _fingerprints(material)
    vault = EphemeralTokenVault(
        maximum_ttl_seconds=3_600,
        expiring_window_seconds=10,
    )
    lease = vault.issue(
        material=material,
        issuer=SyntheticNoNetworkTokenIssuer(ttl_seconds=30),
        purpose=CredentialPurpose.READ_ONLY_PREPARATION,
        account_fingerprint=account,
        client_id_fingerprint=client,
        now=NOW,
        kill_switch=NORMAL,
    )

    assert vault.metadata(
        lease.lease_id,
        now=NOW + timedelta(seconds=30),
    ).state is TokenLeaseState.EXPIRED
    with pytest.raises(CredentialIsolationSafetyError, match="expired"):
        vault.authorize(
            lease_id=lease.lease_id,
            purpose=CredentialPurpose.READ_ONLY_PREPARATION,
            account_fingerprint=account,
            now=NOW + timedelta(seconds=30),
            kill_switch=NORMAL,
        )
    vault.destroy(lease.lease_id)

    halted = KillSwitchState(
        mode=KillSwitchMode.HARD_HALT,
        reason="test halt",
        activated_at=NOW,
    )
    material2 = _material()
    with pytest.raises(CredentialIsolationSafetyError, match="NORMAL"):
        vault.issue(
            material=material2,
            issuer=SyntheticNoNetworkTokenIssuer(),
            purpose=CredentialPurpose.READ_ONLY_PREPARATION,
            account_fingerprint=account,
            client_id_fingerprint=client,
            now=NOW,
            kill_switch=halted,
        )
    material.destroy()
    material2.destroy()
