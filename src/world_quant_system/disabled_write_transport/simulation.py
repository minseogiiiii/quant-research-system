from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from world_quant_system.credential_isolation.ingestion import (
    load_fixture_provider,
)
from world_quant_system.credential_isolation.ingestion import (
    load_policy as load_credential_policy,
)
from world_quant_system.credential_isolation.models import (
    CredentialPurpose,
    sha256_secret,
)
from world_quant_system.credential_isolation.vault import (
    EphemeralTokenVault,
    SyntheticNoNetworkTokenIssuer,
)
from world_quant_system.disabled_write_transport.models import (
    DisabledWriteTransportPolicy,
    DisabledWriteTransportReport,
    HumanApprovalGrant,
    IntegrationDecision,
    TransportOutcome,
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
from world_quant_system.order_write_certification.models import DryRunDecision
from world_quant_system.order_write_certification.service import (
    DeterministicTossOrderWriteDryRunCertifier,
)
from world_quant_system.paper_execution.models import (
    KillSwitchMode,
    KillSwitchState,
)


def _find_project_root() -> Path:
    required = (
        "examples/toss_read_only_certification_report.example.json",
        "examples/toss_order_write_intent.example.json",
        "examples/toss_order_write_account.example.json",
        "examples/toss_order_write_market.example.json",
        "examples/toss_order_write_policy.example.json",
        "examples/credential_isolation_policy.example.json",
        "examples/disabled_write_transport_credential_fixture.example.json",
    )
    for root in (Path.cwd(), *Path.cwd().parents):
        if all((root / relative_path).is_file() for relative_path in required):
            return root
    raise FileNotFoundError(
        "Unable to locate disabled-write transport simulation fixtures."
    )


async def build_simulation_report() -> DisabledWriteTransportReport:
    root = _find_project_root()
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
    assert dry_run.decision is DryRunDecision.HUMAN_APPROVAL_REQUIRED
    assert dry_run.approval_challenge is not None

    credential_policy = load_credential_policy(
        root / "examples/credential_isolation_policy.example.json"
    )
    material = load_fixture_provider(
        root / "examples/disabled_write_transport_credential_fixture.example.json"
    ).load(credential_policy.provider)
    vault = EphemeralTokenVault(
        maximum_ttl_seconds=credential_policy.maximum_token_ttl_seconds,
        expiring_window_seconds=credential_policy.expiring_window_seconds,
    )
    lease_id: str | None = None
    try:
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
        lease_id = issued.lease_id
        token_use = vault.authorize(
            lease_id=lease_id,
            purpose=CredentialPurpose.ORDER_WRITE_DRY_RUN,
            account_fingerprint=account_fingerprint,
            now=approval_at,
            kill_switch=kill_switch,
        )
        active_lease = vault.metadata(lease_id, now=approval_at)
        grant = HumanApprovalGrant.from_challenge(
            dry_run.approval_challenge,
            approver_fingerprint="f" * 64,
            granted_at=approval_at,
        )
        return await DeterministicDisabledWriteTransportIntegrator().certify(
            dry_run_report=dry_run,
            approval_grant=grant,
            token_lease=active_lease,
            token_use=token_use,
            kill_switch=kill_switch,
            policy=DisabledWriteTransportPolicy(),
            evaluated_at=evaluated_at,
        )
    finally:
        if lease_id is not None:
            vault.destroy(lease_id)
        material.destroy()


async def run_simulation() -> None:
    first = await build_simulation_report()
    second = await build_simulation_report()
    assert first.decision is IntegrationDecision.CERTIFIED_BLOCKED
    assert first.report_id == second.report_id
    assert first.report_digest == second.report_digest
    assert first.receipt is not None
    assert first.receipt.outcome is TransportOutcome.WRITE_TRANSPORT_DISABLED
    assert first.network_call_count == 0
    assert first.broker_write_count == 0

    print("Disabled write transport deterministic simulation passed.")
    print("Approval binding: VALIDATED")
    print("Credential lease binding: VALIDATED")
    print("Automatic retries: DISABLED")
    print("Redirects: DISABLED")
    print("External network calls: 0")
    print("Broker write count: 0")
    print("Final state: TRANSPORT_BLOCKED")
    print("Live trading: DISABLED")


def main() -> None:
    asyncio.run(run_simulation())


if __name__ == "__main__":
    main()
