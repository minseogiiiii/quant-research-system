from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from world_quant_system.credential_isolation.ingestion import (
    load_fixture_provider,
    load_policy,
)
from world_quant_system.credential_isolation.models import (
    CredentialCertificationDecision,
    CredentialIsolationError,
    CredentialIsolationReport,
    CredentialPurpose,
    format_utc,
    parse_utc_datetime,
)
from world_quant_system.credential_isolation.providers import (
    EnvironmentSecretProvider,
)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wqs-credential-certify",
        description=(
            "Certify credential memory boundaries with a synthetic, "
            "networkless token. Broker writes remain disabled."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "show-contract",
        help="Show the credential-isolation safety contract.",
    )
    fixture = subparsers.add_parser(
        "certify-fixture",
        help="Certify the boundary with explicitly fake fixture secrets.",
    )
    _add_certification_arguments(fixture)
    fixture.add_argument("--fixture", type=Path, required=True)
    environment = subparsers.add_parser(
        "certify-environment",
        help=(
            "Load fixed WQS_TOSS_* variables into an ephemeral boundary. "
            "No token endpoint or broker endpoint is called."
        ),
    )
    _add_certification_arguments(environment)
    environment.add_argument(
        "--acknowledge-no-network",
        action="store_true",
        required=True,
    )
    return parser


def main() -> None:
    parser = build_parser()
    arguments = parser.parse_args()
    try:
        if arguments.command == "show-contract":
            _print_contract()
            return
        if arguments.command in {"certify-fixture", "certify-environment"}:
            provider = (
                load_fixture_provider(arguments.fixture)
                if arguments.command == "certify-fixture"
                else EnvironmentSecretProvider()
            )
            report = DeterministicCredentialIsolationCertifier().certify(
                provider=provider,
                token_issuer=SyntheticNoNetworkTokenIssuer(),
                policy=load_policy(arguments.policy),
                purpose=CredentialPurpose(arguments.purpose),
                kill_switch=KillSwitchState(
                    mode=KillSwitchMode.NORMAL,
                    reason="normal",
                    activated_at=None,
                ),
                evaluated_at=(
                    datetime.now(UTC)
                    if arguments.evaluated_at is None
                    else parse_utc_datetime(
                        arguments.evaluated_at,
                        "evaluated_at",
                    )
                ),
            )
            print(_format_report(report))
            if arguments.json_output is not None:
                AtomicJsonCredentialIsolationReportWriter(
                    arguments.json_output
                ).write(report)
            if report.decision is not CredentialCertificationDecision.PASSED:
                raise SystemExit(2)
            return
        parser.error(f"Unsupported command: {arguments.command}")
    except CredentialIsolationError as error:
        parser.exit(2, f"ERROR: {error}\n")


def _add_certification_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument(
        "--purpose",
        choices=[purpose.value for purpose in CredentialPurpose],
        default=CredentialPurpose.ORDER_WRITE_DRY_RUN.value,
    )
    parser.add_argument("--evaluated-at", type=str)
    parser.add_argument("--json-output", type=Path)


def _print_contract() -> None:
    print("Credential Boundary & Token Isolation v1")
    print("Secret sources:            ENVIRONMENT or TEST FIXTURE")
    print("Token issuer:              SYNTHETIC / NO NETWORK")
    print("Raw credential storage:    DISABLED")
    print("Raw token storage:         EPHEMERAL MEMORY ONLY")
    print("Authorization output:      Bearer [REDACTED]")
    print("External network:          DISABLED")
    print("Broker writes:             DISABLED")
    print("Live trading:              DISABLED")


def _format_report(report: CredentialIsolationReport) -> str:
    lines = [
        "Credential Boundary & Token Isolation v1",
        f"Evaluated at:             {format_utc(report.evaluated_at)}",
        f"Decision:                 {report.decision.value.upper()}",
        f"Secret source:            {report.credential.source.value.upper()}",
        f"Purpose:                  {report.purpose.value}",
        "Account fingerprint:      "
        f"{report.credential.account_fingerprint}",
        "Client ID fingerprint:    "
        f"{report.credential.client_id_fingerprint}",
        f"Lease state:              {report.lease.state.value.upper()}",
        "Source secrets destroyed: YES",
        "Token secret destroyed:   YES",
        "External network:         DISABLED",
        "Broker write count:       0",
        "Secret persistence:       DISABLED",
        "Live trading:             DISABLED",
        f"Report ID:                {report.report_id}",
        f"Report digest:            {report.report_digest}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
