from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from world_quant_system.order_write_certification.ingestion import (
    load_account_state,
    load_market_state,
    load_order_intent,
    load_policy,
    load_read_only_certification,
)
from world_quant_system.order_write_certification.models import (
    OrderWriteIntegrityError,
)
from world_quant_system.order_write_certification.reporting import (
    AtomicJsonOrderWriteDryRunReportWriter,
)
from world_quant_system.order_write_certification.service import (
    DeterministicTossOrderWriteDryRunCertifier,
)
from world_quant_system.paper_execution.models import (
    KillSwitchMode,
    KillSwitchState,
)

EXAMPLES = Path("examples")


def test_tampered_read_only_report_digest_is_rejected(tmp_path: Path) -> None:
    source = EXAMPLES / "toss_read_only_certification_report.example.json"
    document = json.loads(source.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    document["provider"] = "TAMPERED"
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(OrderWriteIntegrityError):
        load_read_only_certification(path)


def test_tampered_intent_identity_is_rejected(tmp_path: Path) -> None:
    source = EXAMPLES / "toss_order_write_intent.example.json"
    document = json.loads(source.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    document["intent_id"] = "0" * 64
    path = tmp_path / "intent.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(OrderWriteIntegrityError):
        load_order_intent(path)


def test_atomic_report_writer_round_trips(tmp_path: Path) -> None:
    intent = load_order_intent(
        EXAMPLES / "toss_order_write_intent.example.json"
    )
    certification = load_read_only_certification(
        EXAMPLES / "toss_read_only_certification_report.example.json"
    )
    account = load_account_state(
        EXAMPLES / "toss_order_write_account.example.json"
    )
    market = load_market_state(
        EXAMPLES / "toss_order_write_market.example.json"
    )
    policy = load_policy(EXAMPLES / "toss_order_write_policy.example.json")
    report = DeterministicTossOrderWriteDryRunCertifier().certify(
        intent=intent,
        certification=certification,
        account=account,
        market=market,
        policy=policy,
        kill_switch=KillSwitchState(
            mode=KillSwitchMode.NORMAL,
            reason="normal",
            activated_at=None,
        ),
        evaluated_at=datetime(2026, 8, 3, 21, 31, tzinfo=UTC),
    )
    output = tmp_path / "report.json"

    AtomicJsonOrderWriteDryRunReportWriter(output).write(report)

    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["report_digest"] == report.report_digest
    assert document["broker_write_count"] == 0
    assert document["credentials_loaded"] is False
