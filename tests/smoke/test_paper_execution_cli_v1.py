from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from world_quant_system.paper_execution.cli import main
from world_quant_system.paper_execution.models import (
    BrokerCapabilityProfile,
    InternalPaperLedgerSnapshot,
    PaperAccountSnapshot,
    PaperBrokerEnvironment,
    PaperCashBalance,
    PaperExecutionPolicy,
    PaperMarketSnapshot,
    PaperOrderIntent,
    PaperOrderSide,
)


def _write(path: Path, document: dict[str, object]) -> None:
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _documents(now: datetime) -> tuple[
    PaperAccountSnapshot,
    PaperExecutionPolicy,
    PaperMarketSnapshot,
    PaperOrderIntent,
    InternalPaperLedgerSnapshot,
]:
    profile = BrokerCapabilityProfile(
        provider="synthetic-paper",
        environment=PaperBrokerEnvironment.SANDBOX,
        account_fingerprint="a" * 64,
        endpoint_fingerprint="b" * 64,
        currency="USD",
        read_only_access=True,
        paper_order_submission=True,
        cancellation=True,
        client_order_id_idempotency=True,
    )
    cash = PaperCashBalance(
        currency="USD",
        settled_cash=Decimal("100000"),
        buying_power=Decimal("100000"),
    )
    account = PaperAccountSnapshot(
        profile=profile,
        captured_at=now,
        cash=cash,
        positions=(),
        orders=(),
        fills=(),
        source_digest="c" * 64,
    )
    policy = PaperExecutionPolicy(
        expected_provider="synthetic-paper",
        expected_environment=PaperBrokerEnvironment.SANDBOX,
        expected_account_fingerprint="a" * 64,
        expected_endpoint_fingerprint="b" * 64,
        currency="USD",
        allowed_symbols=("ABC",),
        maximum_market_age_seconds=60,
        maximum_account_age_seconds=60,
        maximum_order_quantity=100,
        maximum_order_notional=Decimal("10000"),
        maximum_position_quantity=200,
        maximum_open_orders=5,
        minimum_cash_reserve_fraction=Decimal("0.1"),
        maximum_limit_deviation_bps=Decimal("100"),
        cash_reconciliation_tolerance=Decimal("0.01"),
    )
    market = PaperMarketSnapshot(
        symbol="ABC",
        bid=Decimal("99.9"),
        ask=Decimal("100"),
        last=Decimal("99.95"),
        captured_at=now,
        source_digest="d" * 64,
    )
    intent = PaperOrderIntent(
        decision_id="decision-1",
        symbol="ABC",
        side=PaperOrderSide.BUY,
        quantity=10,
        limit_price=Decimal("100"),
        created_at=now,
        market_snapshot_digest=market.snapshot_digest,
        strategy_id="strategy-1",
        reason="CLI smoke test",
    )
    internal = InternalPaperLedgerSnapshot(
        account_fingerprint="a" * 64,
        captured_at=now,
        cash=cash,
        positions=(),
        orders=(),
        fill_ids=(),
    )
    return account, policy, market, intent, internal


def test_paper_cli_certify_validate_reconcile_and_inspect(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
    account, policy, market, intent, internal = _documents(now)
    account_path = tmp_path / "account.json"
    policy_path = tmp_path / "policy.json"
    market_path = tmp_path / "market.json"
    intent_path = tmp_path / "intent.json"
    internal_path = tmp_path / "internal.json"
    store_path = tmp_path / "paper.sqlite3"
    certify_report = tmp_path / "certify.json"
    decision_report = tmp_path / "decision.json"
    reconcile_report = tmp_path / "reconcile.json"
    inspect_report = tmp_path / "inspect.json"
    _write(account_path, account.to_document())
    _write(policy_path, policy.to_document())
    _write(market_path, market.to_document())
    _write(intent_path, intent.to_document())
    _write(internal_path, internal.to_document())
    timestamp = now.isoformat().replace("+00:00", "Z")

    main(
        [
            "certify",
            "--account-snapshot",
            str(account_path),
            "--policy",
            str(policy_path),
            "--evaluated-at",
            timestamp,
            "--require-order-submission",
            "--json-output",
            str(certify_report),
        ]
    )
    main(
        [
            "validate-intent",
            "--account-snapshot",
            str(account_path),
            "--market-snapshot",
            str(market_path),
            "--intent",
            str(intent_path),
            "--policy",
            str(policy_path),
            "--store",
            str(store_path),
            "--evaluated-at",
            timestamp,
            "--json-output",
            str(decision_report),
        ]
    )
    main(
        [
            "reconcile",
            "--account-snapshot",
            str(account_path),
            "--internal-ledger",
            str(internal_path),
            "--policy",
            str(policy_path),
            "--store",
            str(store_path),
            "--compared-at",
            timestamp,
            "--json-output",
            str(reconcile_report),
        ]
    )
    main(
        [
            "inspect",
            "--store",
            str(store_path),
            "--json-output",
            str(inspect_report),
        ]
    )

    output = capsys.readouterr().out
    assert "Certified: YES" in output
    assert "Pre-trade approved: YES" in output
    assert "Reconciliation: PASS" in output
    assert "Network transport: DISABLED" in output
    assert "Live trading: DISABLED" in output
    assert certify_report.exists()
    assert decision_report.exists()
    assert reconcile_report.exists()
    assert inspect_report.exists()
