import json
from datetime import UTC, datetime
from pathlib import Path

from world_quant_system.credential_isolation.models import (
    CredentialCertificationDecision,
    CredentialIsolationPolicy,
    CredentialIsolationReport,
    CredentialPurpose,
    TokenLeaseState,
    canonical_json_bytes,
)
from world_quant_system.credential_isolation.providers import FakeSecretProvider
from world_quant_system.credential_isolation.redaction import SecretRedactor
from world_quant_system.credential_isolation.reporting import (
    AtomicJsonCredentialIsolationReportWriter,
)
from world_quant_system.credential_isolation.service import (
    DeterministicCredentialIsolationCertifier,
)
from world_quant_system.credential_isolation.vault import (
    SyntheticNoNetworkTokenIssuer,
)
from world_quant_system.paper_execution.models import (
    KillSwitchMode,
    KillSwitchState,
)

NOW = datetime(2026, 8, 3, 22, 30, tzinfo=UTC)


def _report() -> CredentialIsolationReport:
    return DeterministicCredentialIsolationCertifier().certify(
        provider=FakeSecretProvider(
            client_id="fixture-client-id-never-use",
            client_secret="fixture-client-secret-never-use",
            account_id="fixture-account-00000000",
            credential_version="fixture-v1",
        ),
        token_issuer=SyntheticNoNetworkTokenIssuer(ttl_seconds=600),
        policy=CredentialIsolationPolicy(),
        purpose=CredentialPurpose.ORDER_WRITE_DRY_RUN,
        kill_switch=KillSwitchState(
            mode=KillSwitchMode.NORMAL,
            reason="normal",
            activated_at=None,
        ),
        evaluated_at=NOW,
    )


def test_service_is_deterministic_and_persists_no_secret(
    tmp_path: Path,
) -> None:
    first = _report()
    second = _report()

    assert first.decision is CredentialCertificationDecision.PASSED
    assert first.report_id == second.report_id
    assert first.report_digest == second.report_digest
    assert first.lease.state is TokenLeaseState.DESTROYED

    output = tmp_path / "credential-report.json"
    AtomicJsonCredentialIsolationReportWriter(output).write(first)
    payload = output.read_bytes()
    for forbidden in (
        b"fixture-client-id-never-use",
        b"fixture-client-secret-never-use",
        b"fixture-account-00000000",
        b"synthetic.",
    ):
        assert forbidden not in payload
    document = json.loads(payload)
    assert document["source_secrets_destroyed"] is True
    assert document["token_secret_destroyed"] is True
    assert document["broker_write_count"] == 0


def test_redactor_removes_exact_and_pattern_secrets() -> None:
    redactor = SecretRedactor((b"secret-value", b"account-123"))
    text = (
        "client_secret=secret-value Authorization: Bearer abcdefghijklmnop "
        "account_id=account-123"
    )
    redacted = redactor.redact_text(text)

    assert "secret-value" not in redacted
    assert "account-123" not in redacted
    assert "Bearer [REDACTED]" in redacted
    redactor.destroy()
    assert redactor.destroyed


def test_report_document_is_safe_for_canonical_serialization() -> None:
    payload = canonical_json_bytes(_report().to_document())

    assert b"Bearer [REDACTED]" in payload
    assert b"client_secret" not in payload
    assert b"account_id" not in payload
