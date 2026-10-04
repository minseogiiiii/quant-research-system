from __future__ import annotations

from datetime import UTC, datetime

from world_quant_system.credential_isolation.models import (
    CredentialCertificationDecision,
    CredentialFingerprint,
    CredentialIsolationCheck,
    CredentialIsolationPolicy,
    CredentialIsolationReport,
    CredentialIsolationSafetyError,
    CredentialPurpose,
    TokenLeaseState,
    TokenUseReceipt,
    canonical_json_bytes,
    sha256_secret,
)
from world_quant_system.credential_isolation.providers import SecretProvider
from world_quant_system.credential_isolation.redaction import SecretRedactor
from world_quant_system.credential_isolation.vault import (
    EphemeralTokenVault,
    TokenIssuer,
)
from world_quant_system.paper_execution.models import (
    KillSwitchMode,
    KillSwitchState,
)


class DeterministicCredentialIsolationCertifier:
    def certify(
        self,
        *,
        provider: SecretProvider,
        token_issuer: TokenIssuer,
        policy: CredentialIsolationPolicy,
        purpose: CredentialPurpose,
        kill_switch: KillSwitchState,
        evaluated_at: datetime,
    ) -> CredentialIsolationReport:
        _require_aware(evaluated_at)
        if provider.source not in policy.permitted_sources:
            raise CredentialIsolationSafetyError(
                "Secret source is not permitted by policy."
            )
        if purpose not in policy.permitted_purposes:
            raise CredentialIsolationSafetyError(
                "Credential purpose is not permitted by policy."
            )
        if kill_switch.mode is not KillSwitchMode.NORMAL:
            raise CredentialIsolationSafetyError(
                "Credential certification requires a normal kill switch."
            )

        vault = EphemeralTokenVault(
            maximum_ttl_seconds=policy.maximum_token_ttl_seconds,
            expiring_window_seconds=policy.expiring_window_seconds,
        )
        redactor: SecretRedactor | None = None
        credential: CredentialFingerprint | None = None
        lease_id: str | None = None
        receipt: TokenUseReceipt | None = None
        source_destroyed = False
        token_destroyed = False

        material = provider.load(policy.provider)
        try:
            with (
                material.client_id.reveal_bytes() as client_id,
                material.client_secret.reveal_bytes() as client_secret,
                material.account_id.reveal_bytes() as account_id,
                material.credential_version.reveal_bytes() as version,
            ):
                redactor = SecretRedactor(
                    (
                        client_id,
                        client_secret,
                        account_id,
                        version,
                    )
                )
                credential = CredentialFingerprint(
                    provider=policy.provider,
                    source=provider.source,
                    account_fingerprint=sha256_secret(account_id),
                    client_id_fingerprint=sha256_secret(client_id),
                    credential_version_fingerprint=sha256_secret(version),
                    loaded_at=evaluated_at.astimezone(UTC),
                )
            lease = vault.issue(
                material=material,
                issuer=token_issuer,
                purpose=purpose,
                account_fingerprint=credential.account_fingerprint,
                client_id_fingerprint=credential.client_id_fingerprint,
                now=evaluated_at,
                kill_switch=kill_switch,
            )
            lease_id = lease.lease_id
            receipt = vault.authorize(
                lease_id=lease_id,
                purpose=purpose,
                account_fingerprint=credential.account_fingerprint,
                now=evaluated_at,
                kill_switch=kill_switch,
            )
        finally:
            material.destroy()
            source_destroyed = material.destroyed
            if lease_id is not None:
                vault.destroy(lease_id)
                token_destroyed = vault.secret_destroyed(lease_id)

        if credential is None or lease_id is None or receipt is None:
            raise CredentialIsolationSafetyError(
                "Credential certification did not produce an isolated lease."
            )
        destroyed_lease = vault.metadata(lease_id, now=evaluated_at)
        if redactor is None:
            raise CredentialIsolationSafetyError(
                "Credential redaction boundary was not initialized."
            )
        checks = (
            CredentialIsolationCheck(
                name="secret_source_allowed",
                passed=provider.source in policy.permitted_sources,
                detail=f"source={provider.source.value}",
            ),
            CredentialIsolationCheck(
                name="purpose_allowed",
                passed=purpose in policy.permitted_purposes,
                detail=f"purpose={purpose.value}",
            ),
            CredentialIsolationCheck(
                name="kill_switch_normal",
                passed=kill_switch.mode is KillSwitchMode.NORMAL,
                detail=f"kill_switch={kill_switch.mode.value}",
            ),
            CredentialIsolationCheck(
                name="source_secrets_destroyed",
                passed=source_destroyed,
                detail="source credential buffers were zeroized",
            ),
            CredentialIsolationCheck(
                name="token_secret_destroyed",
                passed=token_destroyed,
                detail="ephemeral token buffer was zeroized",
            ),
            CredentialIsolationCheck(
                name="authorization_redacted",
                passed=receipt.authorization_header == "Bearer [REDACTED]",
                detail="only a redacted authorization proof left the vault",
            ),
            CredentialIsolationCheck(
                name="network_disabled",
                passed=not policy.external_network_enabled,
                detail="external network transport is disabled",
            ),
            CredentialIsolationCheck(
                name="broker_writes_disabled",
                passed=not policy.broker_writes_enabled,
                detail="broker write count is zero",
            ),
            CredentialIsolationCheck(
                name="secret_persistence_disabled",
                passed=not policy.secret_persistence_enabled,
                detail="raw credentials and tokens are not persistable",
            ),
        )
        report = CredentialIsolationReport(
            evaluated_at=evaluated_at.astimezone(UTC),
            policy=policy,
            credential=credential,
            purpose=purpose,
            lease=destroyed_lease,
            use_receipt=receipt,
            checks=checks,
            decision=(
                CredentialCertificationDecision.PASSED
                if all(check.passed for check in checks)
                else CredentialCertificationDecision.REJECTED
            ),
            source_secrets_destroyed=source_destroyed,
            token_secret_destroyed=token_destroyed,
        )
        try:
            redactor.assert_safe_bytes(
                canonical_json_bytes(report.to_document())
            )
            if report.lease.state is not TokenLeaseState.DESTROYED:
                raise CredentialIsolationSafetyError(
                    "Credential report retained an active token lease."
                )
            return report
        finally:
            redactor.destroy()


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CredentialIsolationSafetyError(
            "Evaluation timestamp must be timezone-aware."
        )
