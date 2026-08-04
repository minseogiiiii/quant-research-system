from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from world_quant_system.credential_isolation.providers import (
    EnvironmentSecretProvider,
)
from world_quant_system.paper_execution.models import (
    KillSwitchMode,
    KillSwitchState,
)
from world_quant_system.toss_live_read.http_transport import (
    StrictTossHttpsTransport,
)
from world_quant_system.toss_live_read.ingestion import load_policy
from world_quant_system.toss_live_read.models import (
    TossLiveReadCertificationReport,
    TossLiveReadError,
    TossLiveReadPolicy,
)
from world_quant_system.toss_live_read.reporting import (
    AtomicJsonTossLiveReadReportWriter,
)
from world_quant_system.toss_live_read.service import (
    DeterministicTossLiveReadCertifier,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wqs-toss-live-read-certify",
        description=(
            "Perform an explicitly enabled, read-only Toss account certification. "
            "No broker write operation exists in this command."
        ),
    )
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        policy = load_policy(arguments.policy)
        _require_explicit_enable(policy, os.environ)
        material = EnvironmentSecretProvider().load(policy.provider)
        transport = StrictTossHttpsTransport(
            timeout_seconds=policy.timeout_seconds,
            maximum_response_bytes=policy.maximum_response_bytes,
        )
        certifier = DeterministicTossLiveReadCertifier(
            policy=policy,
            transport=transport,
        )
        report = certifier.certify(
            material=material,
            now=datetime.now(UTC),
            kill_switch=KillSwitchState(
                mode=KillSwitchMode.NORMAL,
                reason="normal",
                activated_at=None,
            ),
        )
        AtomicJsonTossLiveReadReportWriter(arguments.output).write(report)
        print(_format_report(report))
    except TossLiveReadError as error:
        parser.exit(2, f"error: {error}\n")


def _require_explicit_enable(
    policy: TossLiveReadPolicy,
    environment: Mapping[str, str],
) -> None:
    if environment.get(policy.enable_variable) != policy.enable_value:
        raise TossLiveReadError(
            "Live read-only access is not explicitly enabled. "
            "No network request was sent."
        )


def _format_report(report: object) -> str:
    if not isinstance(report, TossLiveReadCertificationReport):
        raise TossLiveReadError("Unexpected report type.")
    return "\n".join(
        (
            "Execution mode: LIVE_READ_ONLY_CERTIFICATION",
            "Provider: TOSS",
            "Order submission: DISABLED",
            "Broker write count: 0",
            "",
            f"Decision:              {report.decision.value.upper()}",
            f"Account fingerprint:   {report.account.account_fingerprint}",
            f"Holdings count:        {report.holdings.item_count}",
            f"Open orders count:     {report.open_orders.item_count}",
            f"Closed orders count:   {report.closed_orders.item_count}",
            f"Auth network calls:    {report.auth_network_call_count}",
            f"Read network calls:    {report.read_network_call_count}",
            f"Report ID:             {report.report_id}",
            f"Report digest:         {report.report_digest}",
        )
    )


if __name__ == "__main__":
    main()
