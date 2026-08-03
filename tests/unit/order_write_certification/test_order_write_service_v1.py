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
    DryRunAccountState,
    DryRunDecision,
    DryRunMarketState,
    ReadOnlyCertificationEvidence,
    TossOrderWritePolicy,
)
from world_quant_system.order_write_certification.service import (
    DeterministicTossOrderWriteDryRunCertifier,
)
from world_quant_system.paper_execution.models import (
    KillSwitchMode,
    KillSwitchState,
    PaperOrderIntent,
    PaperOrderSide,
)

EXAMPLES = Path("examples")
EVALUATED_AT = datetime(2026, 8, 3, 21, 31, tzinfo=UTC)


def _inputs() -> tuple[
    PaperOrderIntent,
    ReadOnlyCertificationEvidence,
    DryRunAccountState,
    DryRunMarketState,
    TossOrderWritePolicy,
]:
    return (
        load_order_intent(
            EXAMPLES / "toss_order_write_intent.example.json"
        ),
        load_read_only_certification(
            EXAMPLES
            / "toss_read_only_certification_report.example.json"
        ),
        load_account_state(
            EXAMPLES / "toss_order_write_account.example.json"
        ),
        load_market_state(
            EXAMPLES / "toss_order_write_market.example.json"
        ),
        load_policy(EXAMPLES / "toss_order_write_policy.example.json"),
    )


def _normal_kill_switch() -> KillSwitchState:
    return KillSwitchState(
        mode=KillSwitchMode.NORMAL,
        reason="normal",
        activated_at=None,
    )


def test_successful_dry_run_compiles_exact_contract_without_writes() -> None:
    intent, certification, account, market, policy = _inputs()

    report = DeterministicTossOrderWriteDryRunCertifier().certify(
        intent=intent,
        certification=certification,
        account=account,
        market=market,
        policy=policy,
        kill_switch=_normal_kill_switch(),
        evaluated_at=EVALUATED_AT,
    )

    assert report.decision is DryRunDecision.HUMAN_APPROVAL_REQUIRED
    assert report.compiled_request is not None
    assert report.approval_challenge is not None
    assert report.transport_receipt is not None
    body = dict(report.compiled_request.body)
    assert body == {
        "clientOrderId": intent.client_order_id,
        "symbol": "005930",
        "side": "BUY",
        "orderType": "LIMIT",
        "timeInForce": "DAY",
        "quantity": "1",
        "price": "95000",
        "confirmHighValueOrder": False,
    }
    headers = dict(report.compiled_request.headers)
    assert headers["Authorization"] == "Bearer [REDACTED]"
    assert headers["X-Tossinvest-Account"].startswith("sha256:")
    assert report.transport_receipt.broker_write_count == 0
    assert report.transport_receipt.network_call_count == 0


def test_same_input_is_deterministic() -> None:
    intent, certification, account, market, policy = _inputs()
    certifier = DeterministicTossOrderWriteDryRunCertifier()

    first = certifier.certify(
        intent=intent,
        certification=certification,
        account=account,
        market=market,
        policy=policy,
        kill_switch=_normal_kill_switch(),
        evaluated_at=EVALUATED_AT,
    )
    second = certifier.certify(
        intent=intent,
        certification=certification,
        account=account,
        market=market,
        policy=policy,
        kill_switch=_normal_kill_switch(),
        evaluated_at=EVALUATED_AT,
    )

    assert first.report_id == second.report_id
    assert first.report_digest == second.report_digest
    assert first.compiled_request is not None
    assert second.compiled_request is not None
    assert (
        first.compiled_request.request_digest
        == second.compiled_request.request_digest
    )


def test_kill_switch_rejects_before_compilation() -> None:
    intent, certification, account, market, policy = _inputs()
    kill_switch = KillSwitchState(
        mode=KillSwitchMode.HARD_HALT,
        reason="manual safety halt",
        activated_at=EVALUATED_AT,
    )

    report = DeterministicTossOrderWriteDryRunCertifier().certify(
        intent=intent,
        certification=certification,
        account=account,
        market=market,
        policy=policy,
        kill_switch=kill_switch,
        evaluated_at=EVALUATED_AT,
    )

    assert report.decision is DryRunDecision.REJECTED
    assert report.compiled_request is None
    assert any(
        check.code == "KILL_SWITCH_NORMAL" and not check.passed
        for check in report.checks
    )


def test_stale_read_only_evidence_is_insufficient() -> None:
    intent, certification, account, market, policy = _inputs()
    stale = replace(
        certification,
        certified_at=EVALUATED_AT - timedelta(hours=2),
    )

    report = DeterministicTossOrderWriteDryRunCertifier().certify(
        intent=intent,
        certification=stale,
        account=account,
        market=market,
        policy=policy,
        kill_switch=_normal_kill_switch(),
        evaluated_at=EVALUATED_AT,
    )

    assert report.decision is DryRunDecision.INSUFFICIENT_EVIDENCE
    assert report.compiled_request is None


def test_sell_that_would_create_short_position_is_rejected() -> None:
    intent, certification, account, market, policy = _inputs()
    sell_intent = replace(
        intent,
        side=PaperOrderSide.SELL,
        quantity=4,
        limit_price=market.bid,
    )

    report = DeterministicTossOrderWriteDryRunCertifier().certify(
        intent=sell_intent,
        certification=certification,
        account=account,
        market=market,
        policy=policy,
        kill_switch=_normal_kill_switch(),
        evaluated_at=EVALUATED_AT,
    )

    assert report.decision is DryRunDecision.REJECTED
    assert any(
        check.code == "NO_SHORT_POSITION" and not check.passed
        for check in report.checks
    )


def test_stale_market_is_rejected() -> None:
    intent, certification, account, market, policy = _inputs()
    stale_market = replace(
        market,
        captured_at=EVALUATED_AT - timedelta(minutes=10),
    )
    stale_intent = replace(
        intent,
        market_snapshot_digest=stale_market.snapshot_digest,
    )

    report = DeterministicTossOrderWriteDryRunCertifier().certify(
        intent=stale_intent,
        certification=certification,
        account=account,
        market=stale_market,
        policy=policy,
        kill_switch=_normal_kill_switch(),
        evaluated_at=EVALUATED_AT,
    )

    assert report.decision is DryRunDecision.REJECTED
    assert any(
        check.code == "MARKET_AGE_VALID" and not check.passed
        for check in report.checks
    )
