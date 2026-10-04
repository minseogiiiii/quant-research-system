from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from world_quant_system.credential_isolation.ingestion import (
    load_fixture_provider,
)
from world_quant_system.credential_isolation.ingestion import (
    load_policy as load_credential_policy,
)
from world_quant_system.credential_isolation.models import (
    CredentialPurpose,
    TokenLeaseMetadata,
    TokenUseReceipt,
    sha256_secret,
)
from world_quant_system.credential_isolation.providers import CredentialMaterial
from world_quant_system.credential_isolation.vault import (
    EphemeralTokenVault,
    SyntheticNoNetworkTokenIssuer,
)
from world_quant_system.disabled_write_transport.models import (
    DisabledWriteTransportPolicy,
    DisabledWriteTransportSafetyError,
    HumanApprovalGrant,
    IntegrationDecision,
    RetryDisposition,
    TransportOutcome,
    TransportState,
)
from world_quant_system.disabled_write_transport.service import (
    DeterministicDisabledWriteTransportIntegrator,
)
from world_quant_system.order_write_certification.ingestion import (
    load_account_state,
    load_market_state,
    load_order_intent,
    load_read_only_certification,
)
from world_quant_system.order_write_certification.ingestion import (
    load_policy as load_order_policy,
)
from world_quant_system.order_write_certification.models import (
    OrderWriteDryRunReport,
)
from world_quant_system.order_write_certification.service import (
    DeterministicTossOrderWriteDryRunCertifier,
)
from world_quant_system.paper_execution.models import (
    KillSwitchMode,
    KillSwitchState,
)


@dataclass(frozen=True, slots=True)
class _Case:
    dry_run: OrderWriteDryRunReport
    grant: HumanApprovalGrant
    lease: TokenLeaseMetadata
    token_use: TokenUseReceipt
    kill_switch: KillSwitchState
    evaluated_at: datetime


def _project_root() -> Path:
    for root in (Path.cwd(), *Path.cwd().parents):
        if (
            root
            / "examples/toss_order_write_policy.example.json"
        ).is_file():
            return root
    raise FileNotFoundError("Project examples were not found.")


@contextmanager
def _case() -> Iterator[_Case]:
    root = _project_root()
    dry_run_at = datetime(2026, 8, 3, 21, 31, tzinfo=UTC)
    approval_at = dry_run_at + timedelta(seconds=1)
    evaluated_at = dry_run_at + timedelta(seconds=2)
    kill_switch = KillSwitchState(
        mode=KillSwitchMode.NORMAL,
        reason="normal",
        activated_at=None,
    )
    dry_run = DeterministicTossOrderWriteDryRunCertifier().certify(
        intent=load_order_intent(
            root / "examples/toss_order_write_intent.example.json"
        ),
        certification=load_read_only_certification(
            root
            / "examples/toss_read_only_certification_report.example.json"
        ),
        account=load_account_state(
            root / "examples/toss_order_write_account.example.json"
        ),
        market=load_market_state(
            root / "examples/toss_order_write_market.example.json"
        ),
        policy=load_order_policy(
            root / "examples/toss_order_write_policy.example.json"
        ),
        kill_switch=kill_switch,
        evaluated_at=dry_run_at,
    )
    assert dry_run.approval_challenge is not None
    credential_policy = load_credential_policy(
        root / "examples/credential_isolation_policy.example.json"
    )
    material: CredentialMaterial = load_fixture_provider(
        root / "examples/disabled_write_transport_credential_fixture.example.json"
    ).load(credential_policy.provider)
    vault = EphemeralTokenVault(
        maximum_ttl_seconds=credential_policy.maximum_token_ttl_seconds,
        expiring_window_seconds=credential_policy.expiring_window_seconds,
    )
    with material.account_id.reveal_bytes() as account_id:
        account_fingerprint = sha256_secret(account_id)
    with material.client_id.reveal_bytes() as client_id:
        client_id_fingerprint = sha256_secret(client_id)
    issued = vault.issue(
        material=material,
        issuer=SyntheticNoNetworkTokenIssuer(ttl_seconds=600),
        purpose=CredentialPurpose.ORDER_WRITE_DRY_RUN,
        account_fingerprint=account_fingerprint,
        client_id_fingerprint=client_id_fingerprint,
        now=dry_run_at,
        kill_switch=kill_switch,
    )
    token_use = vault.authorize(
        lease_id=issued.lease_id,
        purpose=CredentialPurpose.ORDER_WRITE_DRY_RUN,
        account_fingerprint=account_fingerprint,
        now=approval_at,
        kill_switch=kill_switch,
    )
    lease = vault.metadata(issued.lease_id, now=approval_at)
    grant = HumanApprovalGrant.from_challenge(
        dry_run.approval_challenge,
        approver_fingerprint="f" * 64,
        granted_at=approval_at,
    )
    try:
        yield _Case(
            dry_run=dry_run,
            grant=grant,
            lease=lease,
            token_use=token_use,
            kill_switch=kill_switch,
            evaluated_at=evaluated_at,
        )
    finally:
        vault.destroy(issued.lease_id)
        material.destroy()


def test_policy_rejects_any_transport_capability() -> None:
    with pytest.raises(DisabledWriteTransportSafetyError):
        DisabledWriteTransportPolicy(allow_redirects=True)
    with pytest.raises(DisabledWriteTransportSafetyError):
        DisabledWriteTransportPolicy(automatic_retries_enabled=True)
    with pytest.raises(DisabledWriteTransportSafetyError):
        DisabledWriteTransportPolicy(external_network_enabled=True)
    with pytest.raises(DisabledWriteTransportSafetyError):
        DisabledWriteTransportPolicy(broker_writes_enabled=True)
    with pytest.raises(DisabledWriteTransportSafetyError):
        DisabledWriteTransportPolicy(submission_states_enabled=True)


def test_human_approval_grant_is_deterministic() -> None:
    with _case() as case:
        challenge = case.dry_run.approval_challenge
        assert challenge is not None
        duplicate = HumanApprovalGrant.from_challenge(
            challenge,
            approver_fingerprint="f" * 64,
            granted_at=case.grant.granted_at,
        )
        assert duplicate == case.grant
        assert duplicate.grant_id == case.grant.grant_id


@pytest.mark.asyncio
async def test_valid_evidence_is_certified_but_terminally_blocked() -> None:
    with _case() as case:
        report = await DeterministicDisabledWriteTransportIntegrator().certify(
            dry_run_report=case.dry_run,
            approval_grant=case.grant,
            token_lease=case.lease,
            token_use=case.token_use,
            kill_switch=case.kill_switch,
            policy=DisabledWriteTransportPolicy(),
            evaluated_at=case.evaluated_at,
        )
        assert report.decision is IntegrationDecision.CERTIFIED_BLOCKED
        assert report.receipt is not None
        assert report.receipt.state is TransportState.TRANSPORT_BLOCKED
        assert (
            report.receipt.outcome
            is TransportOutcome.WRITE_TRANSPORT_DISABLED
        )
        assert (
            report.receipt.retry_disposition
            is RetryDisposition.NO_AUTOMATIC_RETRY
        )
        assert report.network_call_count == 0
        assert report.broker_write_count == 0


@pytest.mark.asyncio
async def test_same_evidence_produces_same_report_identity() -> None:
    with _case() as case:
        integrator = DeterministicDisabledWriteTransportIntegrator()
        first = await integrator.certify(
            dry_run_report=case.dry_run,
            approval_grant=case.grant,
            token_lease=case.lease,
            token_use=case.token_use,
            kill_switch=case.kill_switch,
            policy=DisabledWriteTransportPolicy(),
            evaluated_at=case.evaluated_at,
        )
        second = await integrator.certify(
            dry_run_report=case.dry_run,
            approval_grant=case.grant,
            token_lease=case.lease,
            token_use=case.token_use,
            kill_switch=case.kill_switch,
            policy=DisabledWriteTransportPolicy(),
            evaluated_at=case.evaluated_at,
        )
        assert first.report_id == second.report_id
        assert first.report_digest == second.report_digest
        assert first.receipt == second.receipt


@pytest.mark.asyncio
async def test_expired_approval_is_rejected_without_envelope() -> None:
    with _case() as case:
        report = await DeterministicDisabledWriteTransportIntegrator().certify(
            dry_run_report=case.dry_run,
            approval_grant=case.grant,
            token_lease=case.lease,
            token_use=case.token_use,
            kill_switch=case.kill_switch,
            policy=DisabledWriteTransportPolicy(),
            evaluated_at=case.grant.expires_at,
        )
        assert report.decision is IntegrationDecision.REJECTED
        assert report.envelope is None
        assert report.receipt is None


@pytest.mark.asyncio
async def test_wrong_account_lease_is_rejected() -> None:
    with _case() as case:
        mismatched = replace(case.lease, account_fingerprint="a" * 64)
        report = await DeterministicDisabledWriteTransportIntegrator().certify(
            dry_run_report=case.dry_run,
            approval_grant=case.grant,
            token_lease=mismatched,
            token_use=case.token_use,
            kill_switch=case.kill_switch,
            policy=DisabledWriteTransportPolicy(),
            evaluated_at=case.evaluated_at,
        )
        assert report.decision is IntegrationDecision.REJECTED
        assert report.broker_write_count == 0


@pytest.mark.asyncio
async def test_wrong_credential_purpose_is_rejected() -> None:
    with _case() as case:
        mismatched_lease = replace(
            case.lease,
            purpose=CredentialPurpose.READ_ONLY_PREPARATION,
        )
        mismatched_use = replace(
            case.token_use,
            purpose=CredentialPurpose.READ_ONLY_PREPARATION,
        )
        report = await DeterministicDisabledWriteTransportIntegrator().certify(
            dry_run_report=case.dry_run,
            approval_grant=case.grant,
            token_lease=mismatched_lease,
            token_use=mismatched_use,
            kill_switch=case.kill_switch,
            policy=DisabledWriteTransportPolicy(),
            evaluated_at=case.evaluated_at,
        )
        assert report.decision is IntegrationDecision.REJECTED
        assert report.network_call_count == 0


@pytest.mark.asyncio
async def test_non_normal_kill_switch_is_rejected() -> None:
    with _case() as case:
        halted = KillSwitchState(
            mode=KillSwitchMode.HARD_HALT,
            reason="test halt",
            activated_at=case.evaluated_at,
        )
        report = await DeterministicDisabledWriteTransportIntegrator().certify(
            dry_run_report=case.dry_run,
            approval_grant=case.grant,
            token_lease=case.lease,
            token_use=case.token_use,
            kill_switch=halted,
            policy=DisabledWriteTransportPolicy(),
            evaluated_at=case.evaluated_at,
        )
        assert report.decision is IntegrationDecision.REJECTED
        assert report.envelope is None


@pytest.mark.asyncio
async def test_serialized_report_contains_no_fixture_secrets() -> None:
    with _case() as case:
        report = await DeterministicDisabledWriteTransportIntegrator().certify(
            dry_run_report=case.dry_run,
            approval_grant=case.grant,
            token_lease=case.lease,
            token_use=case.token_use,
            kill_switch=case.kill_switch,
            policy=DisabledWriteTransportPolicy(),
            evaluated_at=case.evaluated_at,
        )
        serialized = json.dumps(report.to_document(), sort_keys=True)
        assert "fixture-client-secret-never-use" not in serialized
        assert "fixture-account-00000000" not in serialized
        assert "synthetic." not in serialized
        assert "Bearer [REDACTED]" in serialized
