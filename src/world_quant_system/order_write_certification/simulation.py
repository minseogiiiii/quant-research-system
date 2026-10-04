from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from world_quant_system.order_write_certification.ingestion import (
    load_account_state,
    load_market_state,
    load_order_intent,
    load_policy,
    load_read_only_certification,
)
from world_quant_system.order_write_certification.models import (
    DryRunDecision,
)
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
    )
    for root in (Path.cwd(), *Path.cwd().parents):
        if all((root / relative_path).is_file() for relative_path in required):
            return root
    raise FileNotFoundError(
        "Unable to locate order-write certification example fixtures."
    )


def main() -> None:
    root = _find_project_root()
    evaluated_at = datetime(2026, 8, 3, 21, 31, tzinfo=UTC)
    certification = load_read_only_certification(
        root
        / "examples/toss_read_only_certification_report.example.json"
    )
    intent = load_order_intent(
        root / "examples/toss_order_write_intent.example.json"
    )
    account = load_account_state(
        root / "examples/toss_order_write_account.example.json"
    )
    market = load_market_state(
        root / "examples/toss_order_write_market.example.json"
    )
    policy = load_policy(
        root / "examples/toss_order_write_policy.example.json"
    )
    kill_switch = KillSwitchState(
        mode=KillSwitchMode.NORMAL,
        reason="normal",
        activated_at=None,
    )
    certifier = DeterministicTossOrderWriteDryRunCertifier()
    first = certifier.certify(
        intent=intent,
        certification=certification,
        account=account,
        market=market,
        policy=policy,
        kill_switch=kill_switch,
        evaluated_at=evaluated_at,
    )
    second = certifier.certify(
        intent=intent,
        certification=certification,
        account=account,
        market=market,
        policy=policy,
        kill_switch=kill_switch,
        evaluated_at=evaluated_at,
    )
    assert first.decision is DryRunDecision.HUMAN_APPROVAL_REQUIRED
    assert first.report_id == second.report_id
    assert first.report_digest == second.report_digest
    assert first.compiled_request is not None
    assert first.transport_receipt is not None
    assert first.transport_receipt.broker_write_count == 0
    assert first.transport_receipt.network_call_count == 0
    assert first.compiled_request.network_sendable is False
    assert first.compiled_request.credentials_loaded is False

    stale_market = replace(
        market,
        captured_at=evaluated_at - timedelta(hours=1),
    )
    stale_intent = replace(
        intent,
        market_snapshot_digest=stale_market.snapshot_digest,
    )
    rejected = certifier.certify(
        intent=stale_intent,
        certification=certification,
        account=account,
        market=stale_market,
        policy=policy,
        kill_switch=kill_switch,
        evaluated_at=evaluated_at,
    )
    assert rejected.decision is DryRunDecision.REJECTED
    assert rejected.compiled_request is None
    print("Toss order-write dry-run deterministic simulation passed.")
    print("External network transport: DISABLED")
    print("Credentials loaded: NO")
    print("Broker write count: 0")
    print("Order submission: NOT SUBMITTED")
    print("Live trading: DISABLED")


if __name__ == "__main__":
    main()
