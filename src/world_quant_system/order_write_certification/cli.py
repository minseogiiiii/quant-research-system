from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from world_quant_system.order_write_certification.ingestion import (
    load_account_state,
    load_kill_switch,
    load_market_state,
    load_order_intent,
    load_policy,
    load_read_only_certification,
)
from world_quant_system.order_write_certification.models import (
    OFFICIAL_CLIENT_ORDER_ID_TTL_SECONDS,
    OFFICIAL_ORDER_CREATE_METHOD,
    OFFICIAL_ORDER_CREATE_PATH,
    OFFICIAL_TOSS_BASE_URL,
    PINNED_OPENAPI_VERSION,
    DryRunDecision,
    OrderWriteCertificationError,
    OrderWriteDryRunReport,
    TossOrderCreateContract,
    format_utc,
    parse_utc_datetime,
)
from world_quant_system.order_write_certification.reporting import (
    AtomicJsonOrderWriteDryRunReportWriter,
)
from world_quant_system.order_write_certification.service import (
    DeterministicTossOrderWriteDryRunCertifier,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wqs-order-write-certify",
        description=(
            "Compile and certify a non-sendable Toss order-create request. "
            "Credentials, network transport, and broker writes remain disabled."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "show-contract",
        help="Show the pinned order-create contract and disabled capabilities.",
    )
    certify = subparsers.add_parser(
        "certify-dry-run",
        help="Run fail-closed order-write certification without submission.",
    )
    certify.add_argument("--read-only-report", type=Path, required=True)
    certify.add_argument("--intent", type=Path, required=True)
    certify.add_argument("--account", type=Path, required=True)
    certify.add_argument("--market", type=Path, required=True)
    certify.add_argument("--policy", type=Path, required=True)
    certify.add_argument("--kill-switch", type=Path)
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
        if arguments.command == "certify-dry-run":
            report = DeterministicTossOrderWriteDryRunCertifier().certify(
                intent=load_order_intent(arguments.intent),
                certification=load_read_only_certification(
                    arguments.read_only_report
                ),
                account=load_account_state(arguments.account),
                market=load_market_state(arguments.market),
                policy=load_policy(arguments.policy),
                kill_switch=load_kill_switch(arguments.kill_switch),
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
                AtomicJsonOrderWriteDryRunReportWriter(
                    arguments.json_output
                ).write(report)
            if report.decision is not DryRunDecision.HUMAN_APPROVAL_REQUIRED:
                raise SystemExit(2)
            return
        parser.error(f"Unsupported command: {arguments.command}")
    except OrderWriteCertificationError as error:
        parser.exit(2, f"ERROR: {error}\n")


def _print_contract() -> None:
    contract = TossOrderCreateContract()
    print("Toss Order Write Dry-Run Contract v1")
    print(f"OpenAPI version:          {PINNED_OPENAPI_VERSION}")
    print(f"Base URL:                 {OFFICIAL_TOSS_BASE_URL}")
    print(
        "Create order:             "
        f"{OFFICIAL_ORDER_CREATE_METHOD} {OFFICIAL_ORDER_CREATE_PATH}"
    )
    print(
        "clientOrderId TTL:        "
        f"{OFFICIAL_CLIENT_ORDER_ID_TTL_SECONDS} seconds"
    )
    print("Supported request:        quantity-based LIMIT + DAY")
    print("Credentials:              DISABLED")
    print("External network:         DISABLED")
    print("Broker writes:            DISABLED")
    print("Live trading:             DISABLED")
    print(f"Contract digest:          {contract.contract_digest}")


def _format_report(report: OrderWriteDryRunReport) -> str:
    failed_count = sum(not check.passed for check in report.checks)
    request_digest = (
        "NONE"
        if report.compiled_request is None
        else report.compiled_request.request_digest
    )
    approval_id = (
        "NONE"
        if report.approval_challenge is None
        else report.approval_challenge.approval_id
    )
    lines = [
        "Toss Order Write Dry-Run & Safety Certification v1",
        f"Evaluated at:             {format_utc(report.evaluated_at)}",
        f"Decision:                 {report.decision.value.upper()}",
        f"Checks:                   {len(report.checks)}",
        f"Failed checks:            {failed_count}",
        f"Request digest:           {request_digest}",
        f"Approval challenge:       {approval_id}",
        "External network:         DISABLED",
        "Credentials loaded:       NO",
        "Broker write count:       0",
        "Order submission:         NOT SUBMITTED",
        "Live trading:             DISABLED",
        f"Report ID:                {report.report_id}",
        f"Report digest:            {report.report_digest}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
