from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, Protocol

from world_quant_system.paper_execution.gateway import (
    BrokerCertificationService,
    PreTradeRiskGateway,
)
from world_quant_system.paper_execution.models import (
    PaperExecutionConfigurationError,
    PaperExecutionError,
    parse_account_snapshot,
    parse_internal_ledger,
    parse_market_snapshot,
    parse_order_intent,
    parse_policy,
    parse_utc_datetime,
)
from world_quant_system.paper_execution.reconciliation import (
    DeterministicPaperReconciler,
)
from world_quant_system.paper_execution.reporting import (
    AtomicJsonPaperReportWriter,
    load_json_document,
)
from world_quant_system.paper_execution.store import SQLitePaperExecutionStore


class _DocumentSerializable(Protocol):
    def to_document(self) -> dict[str, object]:
        """Return a JSON-compatible document."""
        ...


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wqs-paper",
        description=(
            "Offline paper-broker certification, pre-trade validation, and "
            "reconciliation. No network transport or live-order path exists."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    certify = subparsers.add_parser(
        "certify",
        help="certify an offline sandbox/paper account snapshot",
    )
    _add_account_policy_arguments(certify)
    certify.add_argument("--evaluated-at")
    certify.add_argument(
        "--require-order-submission",
        action="store_true",
        help="require paper submission, cancellation, and client-ID idempotency",
    )
    certify.add_argument("--json-output", type=Path)

    validate = subparsers.add_parser(
        "validate-intent",
        help="run pre-trade gates without submitting an order",
    )
    _add_account_policy_arguments(validate)
    validate.add_argument("--market-snapshot", type=Path, required=True)
    validate.add_argument("--intent", type=Path, required=True)
    validate.add_argument("--store", type=Path, required=True)
    validate.add_argument("--evaluated-at")
    validate.add_argument("--json-output", type=Path)

    reconcile = subparsers.add_parser(
        "reconcile",
        help="compare internal and broker paper state and fail closed",
    )
    _add_account_policy_arguments(reconcile)
    reconcile.add_argument("--internal-ledger", type=Path, required=True)
    reconcile.add_argument("--store", type=Path, required=True)
    reconcile.add_argument("--compared-at")
    reconcile.add_argument("--json-output", type=Path)

    inspect = subparsers.add_parser(
        "inspect",
        help="inspect the local paper execution store",
    )
    inspect.add_argument("--store", type=Path, required=True)
    inspect.add_argument("--json-output", type=Path)

    reset = subparsers.add_parser(
        "reset-kill-switch",
        help="explicitly reset a reviewed local kill switch",
    )
    reset.add_argument("--store", type=Path, required=True)
    reset.add_argument("--expected-version", type=int, required=True)
    reset.add_argument("--reason", required=True)
    reset.add_argument("--reset-at")
    reset.add_argument("--json-output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "certify":
            _run_certify(arguments)
            return
        if arguments.command == "validate-intent":
            _run_validate_intent(arguments)
            return
        if arguments.command == "reconcile":
            _run_reconcile(arguments)
            return
        if arguments.command == "inspect":
            _run_inspect(arguments)
            return
        if arguments.command == "reset-kill-switch":
            _run_reset_kill_switch(arguments)
            return
        _unreachable()
    except PaperExecutionError as error:
        parser.error(str(error))


def _run_certify(arguments: argparse.Namespace) -> None:
    account = parse_account_snapshot(
        load_json_document(arguments.account_snapshot)
    )
    policy = parse_policy(load_json_document(arguments.policy))
    evaluated_at = _optional_timestamp(arguments.evaluated_at)
    report = BrokerCertificationService().certify(
        account=account,
        policy=policy,
        evaluated_at=evaluated_at,
        require_order_submission=arguments.require_order_submission,
    )
    _write_optional(arguments.json_output, report)
    print("Execution mode: PAPER_SAFETY_FOUNDATION")
    print(f"Broker provider: {account.profile.provider}")
    print(f"Broker environment: {account.profile.environment.value}")
    print(f"Certified: {'YES' if report.certified else 'NO'}")
    print("Network transport: DISABLED")
    print("Order submission from CLI: DISABLED")
    print("Live trading: DISABLED")
    if report.reasons:
        print("Reasons: " + ", ".join(report.reasons))


def _run_validate_intent(arguments: argparse.Namespace) -> None:
    account = parse_account_snapshot(
        load_json_document(arguments.account_snapshot)
    )
    market = parse_market_snapshot(
        load_json_document(arguments.market_snapshot)
    )
    intent = parse_order_intent(load_json_document(arguments.intent))
    policy = parse_policy(load_json_document(arguments.policy))
    store = SQLitePaperExecutionStore(arguments.store)
    decision = PreTradeRiskGateway().evaluate(
        intent=intent,
        market=market,
        account=account,
        policy=policy,
        kill_switch_mode=store.get_kill_switch().mode,
        evaluated_at=_optional_timestamp(arguments.evaluated_at),
        existing_client_order_ids=frozenset(
            item.intent.client_order_id for item in store.list_orders()
        ),
    )
    _write_optional(arguments.json_output, decision)
    print("Execution mode: PAPER_SAFETY_FOUNDATION")
    print(f"Client order ID: {decision.client_order_id}")
    print(f"Pre-trade approved: {'YES' if decision.approved else 'NO'}")
    print("Broker write performed: NO")
    print("Live trading: DISABLED")
    if decision.reasons:
        print("Reasons: " + ", ".join(decision.reasons))


def _run_reconcile(arguments: argparse.Namespace) -> None:
    internal = parse_internal_ledger(
        load_json_document(arguments.internal_ledger)
    )
    account = parse_account_snapshot(
        load_json_document(arguments.account_snapshot)
    )
    policy = parse_policy(load_json_document(arguments.policy))
    store = SQLitePaperExecutionStore(arguments.store)
    report = DeterministicPaperReconciler().reconcile_and_record(
        internal=internal,
        broker=account,
        policy=policy,
        compared_at=_optional_timestamp(arguments.compared_at),
        store=store,
    )
    _write_optional(arguments.json_output, report)
    print("Execution mode: PAPER_SAFETY_FOUNDATION")
    print(f"Reconciliation: {report.decision.value.upper()}")
    print(
        "Recommended kill switch: "
        f"{report.recommended_kill_switch.value.upper()}"
    )
    print(f"Discrepancies: {len(report.discrepancies)}")
    print("Automatic broker-state overwrite: DISABLED")
    print("Live trading: DISABLED")


def _run_inspect(arguments: argparse.Namespace) -> None:
    store = SQLitePaperExecutionStore(arguments.store)
    kill_switch = store.get_kill_switch()
    orders = store.list_orders()
    events = store.list_events()
    document: dict[str, object] = {
        "kill_switch": kill_switch.to_document(),
        "orders": [item.to_document() for item in orders],
        "events": [item.to_document() for item in events],
        "latest_reconciliation": store.latest_reconciliation_document(),
        "network_transport": "disabled",
        "live_trading": "disabled",
    }
    _write_optional(arguments.json_output, document)
    print("Execution mode: PAPER_SAFETY_FOUNDATION")
    print(f"Kill switch: {kill_switch.mode.value.upper()}")
    print(f"Orders: {len(orders)}")
    print(f"Events: {len(events)}")
    print("Network transport: DISABLED")
    print("Live trading: DISABLED")


def _run_reset_kill_switch(arguments: argparse.Namespace) -> None:
    if arguments.expected_version < 0:
        raise PaperExecutionConfigurationError(
            "Expected version cannot be negative."
        )
    store = SQLitePaperExecutionStore(arguments.store)
    state = store.reset_kill_switch(
        reason=arguments.reason,
        reset_at=_optional_timestamp(arguments.reset_at),
        expected_version=arguments.expected_version,
    )
    _write_optional(arguments.json_output, state.to_document())
    print("Execution mode: PAPER_SAFETY_FOUNDATION")
    print(f"Kill switch: {state.mode.value.upper()}")
    print("Reset requires explicit version and review reason: YES")
    print("Live trading: DISABLED")


def _add_account_policy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account-snapshot", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)


def _optional_timestamp(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return parse_utc_datetime(value, "CLI timestamp")


def _write_optional(
    path: Path | None,
    document: _DocumentSerializable | dict[str, object],
) -> None:
    if path is None:
        return
    payload = document if isinstance(document, dict) else document.to_document()
    AtomicJsonPaperReportWriter(path).write(payload)


def _unreachable() -> NoReturn:
    raise AssertionError("Unreachable paper CLI command state.")


if __name__ == "__main__":
    main(sys.argv[1:])
