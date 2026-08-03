from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from world_quant_system.broker_certification.ingestion import (
    load_capture_bundle,
    load_policy,
)
from world_quant_system.broker_certification.models import (
    OFFICIAL_READ_ONLY_ENDPOINTS,
    AdapterCertificationReport,
    BrokerAdapterCertificationError,
    CertificationStatus,
    format_utc,
    parse_utc_datetime,
)
from world_quant_system.broker_certification.reporting import (
    AtomicJsonAdapterCertificationReportWriter,
)
from world_quant_system.broker_certification.service import (
    DeterministicReadOnlyAdapterCertifier,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wqs-broker-certify",
        description=(
            "Offline certification for the official Toss Open API read-only "
            "account, holdings, and order-history contract."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "show-contract",
        help="Show the fixed GET-only endpoint allowlist.",
    )

    certify = subparsers.add_parser(
        "certify-captures",
        help="Certify captured JSON responses without enabling network access.",
    )
    certify.add_argument("--policy", type=Path, required=True)
    certify.add_argument("--captures", type=Path, required=True)
    certify.add_argument("--certified-at", type=str)
    certify.add_argument("--json-output", type=Path)
    return parser


def main() -> None:
    parser = build_parser()
    arguments = parser.parse_args()
    try:
        if arguments.command == "show-contract":
            _print_contract()
            return
        if arguments.command == "certify-captures":
            policy = load_policy(arguments.policy)
            capture = load_capture_bundle(arguments.captures)
            certified_at = (
                datetime.now(UTC)
                if arguments.certified_at is None
                else parse_utc_datetime(arguments.certified_at, "certified_at")
            )
            report = DeterministicReadOnlyAdapterCertifier().certify(
                policy=policy,
                capture=capture,
                certified_at=certified_at,
            )
            print(_format_report(report))
            if arguments.json_output is not None:
                AtomicJsonAdapterCertificationReportWriter(
                    arguments.json_output
                ).write(report)
            if report.status is CertificationStatus.FAIL:
                raise SystemExit(2)
            return
        parser.error(f"Unsupported command: {arguments.command}")
    except BrokerAdapterCertificationError as error:
        parser.exit(2, f"ERROR: {error}\n")


def _print_contract() -> None:
    print("Provider: TOSSINVEST")
    print("Base URL: https://openapi.tossinvest.com")
    print("Network transport: DISABLED")
    print("Write operations: DISABLED")
    print("Read-only operations:")
    for endpoint in OFFICIAL_READ_ONLY_ENDPOINTS:
        query = ""
        if endpoint.fixed_query:
            query = "?" + "&".join(
                f"{key}={value}" for key, value in endpoint.fixed_query
            )
        account_scope = (
            "account-header-required"
            if endpoint.account_header_required
            else "token-only"
        )
        print(
            f"- GET {endpoint.path}{query} | "
            f"{endpoint.rate_limit_group} | {account_scope}"
        )


def _format_report(report: AdapterCertificationReport) -> str:
    critical_count = sum(
        finding.severity.value == "critical" for finding in report.findings
    )
    warning_count = sum(
        finding.severity.value == "warning" for finding in report.findings
    )
    lines = [
        "Toss Read-Only Adapter Contract Certification v1",
        f"Certified at:             {format_utc(report.certified_at)}",
        f"Status:                   {report.status.value.upper()}",
        f"Operations certified:     {len(report.operations)}",
        f"Accounts normalized:      {len(report.accounts)}",
        f"Holdings normalized:      {len(report.holdings)}",
        f"Orders normalized:        {len(report.orders)}",
        f"Critical findings:        {critical_count}",
        f"Warnings:                 {warning_count}",
        "Network transport:        DISABLED",
        "Write operations:         DISABLED",
        f"Report ID:                {report.report_id}",
        f"Report digest:            {report.report_digest}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
