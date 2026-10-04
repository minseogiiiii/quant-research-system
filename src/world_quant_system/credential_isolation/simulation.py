from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from world_quant_system.credential_isolation.ingestion import (
    load_fixture_provider,
    load_policy,
)
from world_quant_system.credential_isolation.models import (
    CredentialCertificationDecision,
    CredentialPurpose,
    TokenLeaseState,
    canonical_json_bytes,
    sha256_secret,
)
from world_quant_system.credential_isolation.service import (
    DeterministicCredentialIsolationCertifier,
)
from world_quant_system.credential_isolation.vault import (
    EphemeralTokenVault,
    SyntheticNoNetworkTokenIssuer,
)
from world_quant_system.paper_execution.models import (
    KillSwitchMode,
    KillSwitchState,
)


def _find_project_root() -> Path:
    required = (
        "examples/credential_isolation_policy.example.json",
        "examples/credential_isolation_fixture.example.json",
    )
    for root in (Path.cwd(), *Path.cwd().parents):
        if all((root / relative_path).is_file() for relative_path in required):
            return root
    raise FileNotFoundError(
        "Unable to locate credential-isolation example fixtures."
    )


def main() -> None:
    root = _find_project_root()
    evaluated_at = datetime(2026, 8, 3, 22, 30, tzinfo=UTC)
    policy = load_policy(
        root / "examples/credential_isolation_policy.example.json"
    )
    provider = load_fixture_provider(
        root / "examples/credential_isolation_fixture.example.json"
    )
    kill_switch = KillSwitchState(
        mode=KillSwitchMode.NORMAL,
        reason="normal",
        activated_at=None,
    )
    certifier = DeterministicCredentialIsolationCertifier()
    first = certifier.certify(
        provider=provider,
        token_issuer=SyntheticNoNetworkTokenIssuer(ttl_seconds=600),
        policy=policy,
        purpose=CredentialPurpose.ORDER_WRITE_DRY_RUN,
        kill_switch=kill_switch,
        evaluated_at=evaluated_at,
    )
    second = certifier.certify(
        provider=provider,
        token_issuer=SyntheticNoNetworkTokenIssuer(ttl_seconds=600),
        policy=policy,
        purpose=CredentialPurpose.ORDER_WRITE_DRY_RUN,
        kill_switch=kill_switch,
        evaluated_at=evaluated_at,
    )
    assert first.decision is CredentialCertificationDecision.PASSED
    assert first.report_id == second.report_id
    assert first.report_digest == second.report_digest
    assert first.lease.state is TokenLeaseState.DESTROYED
    assert first.source_secrets_destroyed
    assert first.token_secret_destroyed
    serialized = canonical_json_bytes(first.to_document())
    for forbidden in (
        b"fixture-client-secret-never-use",
        b"fixture-account-00000000",
        b"synthetic.",
    ):
        assert forbidden not in serialized

    vault = EphemeralTokenVault(
        maximum_ttl_seconds=policy.maximum_token_ttl_seconds,
        expiring_window_seconds=policy.expiring_window_seconds,
    )
    material = provider.load(policy.provider)
    with material.account_id.reveal_bytes() as account_id:
        account_fingerprint = sha256_secret(account_id)
    with material.client_id.reveal_bytes() as client_id:
        client_id_fingerprint = sha256_secret(client_id)
    lease = vault.issue(
        material=material,
        issuer=SyntheticNoNetworkTokenIssuer(ttl_seconds=60),
        purpose=CredentialPurpose.READ_ONLY_PREPARATION,
        account_fingerprint=account_fingerprint,
        client_id_fingerprint=client_id_fingerprint,
        now=evaluated_at,
        kill_switch=kill_switch,
    )
    expired = vault.metadata(
        lease.lease_id,
        now=evaluated_at + timedelta(seconds=61),
    )
    assert expired.state is TokenLeaseState.EXPIRED
    vault.destroy(lease.lease_id)
    material.destroy()

    print("Credential isolation deterministic simulation passed.")
    print("Synthetic token issuer: ENABLED FOR CERTIFICATION ONLY")
    print("External network transport: DISABLED")
    print("Raw credential persistence: DISABLED")
    print("Raw token persistence: DISABLED")
    print("Broker write count: 0")
    print("Live trading: DISABLED")


if __name__ == "__main__":
    main()
