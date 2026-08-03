from __future__ import annotations

import argparse
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
from world_quant_system.disabled_write_transport.ingestion import (
    load_approver_fingerprint,
)
from world_quant_system.disabled_write_transport.ingestion import (
    load_policy as load_transport_policy,
)
from world_quant_system.disabled_write_transport.models import (
    DisabledWriteTransportError,
    DisabledWriteTransportReport,
    HumanApprovalGrant,
    IntegrationDecision,
    format_utc,
)
from world_quant_system.disabled_write_transport.reporting import (
    AtomicJsonDisabledWriteTransportReportWriter,
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
    DryRunDecision,
    parse_utc_datetime,
)
from world_quant_system.order_write_certification.service import (
    DeterministicTossOrderWriteDryRunCertifier,
)
from world_quant_system.paper_execution.models import (
    KillSwitchMode,
    KillSwitchState,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wqs-write-transport-certify",
        description=(
            "Bind dry-run, approval, and credential evidence to a terminal "
            "transport that always blocks broker writes."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "show-contract",
        help="Show the disabled write-transport safety contract.",
    )
    certify = subparsers.add_parser(
        "certify-fixture",
        help="Run the complete no-network integration with fake credentials.",
    )
    certify.add_argument("--read-only-certification", type=Path, required=True)
    certify.add_argument("--intent", type=Path, required=True)
    certify.add_argument("--account", type=Path, required=True)
    certify.add_argument("--market", type=Path, required=True)
    certify.add_argument("--order-policy", type=Path, required=True)
    certify.add_argument("--credential-policy", type=Path, required=True)
    certify.add_argument("--credential-fixture", type=Path, required=True)
    certify.add_argument("--transport-policy", type=Path, required=True)
    certify.add_argument("--approval", type=Path, required=True)
    certify.add_argument("--evaluated-at", type=str)
    certify.add_argument("--json-output", type=Path)
    return parser


def main() -> None:
    parser = build_parser()
    arguments = parser.parse_args()
    try:
        if arguments.command == "show-contract":
            _print_contract()
            return
        if arguments.command == "certify-fixture":
            report = asyncio.run(_certify_fixture(arguments))
            print(_format_report(report))
            if arguments.json_output is not None:
                AtomicJsonDisabledWriteTransportReportWriter(
                    arguments.json_output
                ).write(report)
            if report.decision is not IntegrationDecision.CERTIFIED_BLOCKED:
                raise SystemExit(2)
            return
        parser.error(f"Unsupported command: {arguments.command}")
    except DisabledWriteTransportError as error:
        parser.exit(2, f"ERROR: {error}\n")


async def _certify_fixture(
    arguments: argparse.Namespace,
) -> DisabledWriteTransportReport:
    evaluated_at = (
        datetime.now(UTC)
        if arguments.evaluated_at is None
        else parse_utc_datetime(arguments.evaluated_at, "evaluated_at")
    )
    dry_run_at = evaluated_at - timedelta(seconds=2)
    approval_at = evaluated_at - timedelta(seconds=1)
    kill_switch = KillSwitchState(
        mode=KillSwitchMode.NORMAL,
        reason="normal",
        activated_at=None,
    )
    dry_run = DeterministicTossOrderWriteDryRunCertifier().certify(
        intent=load_order_intent(arguments.intent),
        certification=load_read_only_certification(
            arguments.read_only_certification
        ),
        account=load_account_state(arguments.account),
        market=load_market_state(arguments.market),
        policy=load_order_policy(arguments.order_policy),
        kill_switch=kill_switch,
        evaluated_at=dry_run_at,
    )
    if dry_run.decision is not DryRunDecision.HUMAN_APPROVAL_REQUIRED:
        raise DisabledWriteTransportError(
            "Upstream dry-run report did not reach human approval."
        )
    if dry_run.approval_challenge is None:
        raise DisabledWriteTransportError(
            "Upstream dry-run report is missing its approval challenge."
        )
    credential_policy = load_credential_policy(arguments.credential_policy)
    material = load_fixture_provider(arguments.credential_fixture).load(
        credential_policy.provider
    )
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
            approver_fingerprint=load_approver_fingerprint(arguments.approval),
            granted_at=approval_at,
        )
        return await DeterministicDisabledWriteTransportIntegrator().certify(
            dry_run_report=dry_run,
            approval_grant=grant,
            token_lease=active_lease,
            token_use=token_use,
            kill_switch=kill_switch,
            policy=load_transport_policy(arguments.transport_policy),
            evaluated_at=evaluated_at,
        )
    finally:
        if lease_id is not None:
            vault.destroy(lease_id)
        material.destroy()


def _print_contract() -> None:
    print("Disabled Write Transport Integration v1")
    print("Pinned host:              https://openapi.tossinvest.com")
    print("Pinned operation:         POST /api/v1/orders")
    print("Approval scope:           ONE ORDER CREATE")
    print("Credential purpose:       ORDER_WRITE_DRY_RUN")
    print("Automatic retries:        DISABLED")
    print("Redirects:                DISABLED")
    print("External network:         DISABLED")
    print("Broker writes:            DISABLED")
    print("Final state:              TRANSPORT_BLOCKED")
    print("Live trading:             DISABLED")


def _format_report(report: DisabledWriteTransportReport) -> str:
    receipt_state = (
        "NONE" if report.receipt is None else report.receipt.state.value.upper()
    )
    return "\n".join(
        (
            "Disabled Write Transport Integration v1",
            f"Evaluated at:             {format_utc(report.evaluated_at)}",
            f"Decision:                 {report.decision.value.upper()}",
            f"Final state:              {receipt_state}",
            "Automatic retries:        DISABLED",
            "Redirects:                DISABLED",
            f"External network calls:   {report.network_call_count}",
            f"Broker write count:       {report.broker_write_count}",
            "Live trading:             DISABLED",
            f"Report ID:                {report.report_id}",
            f"Report digest:            {report.report_digest}",
        )
    )


if __name__ == "__main__":
    main()
