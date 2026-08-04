from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from world_quant_system.credential_isolation.providers import FakeSecretProvider
from world_quant_system.paper_execution.models import (
    KillSwitchMode,
    KillSwitchState,
)
from world_quant_system.toss_live_read.ingestion import load_policy
from world_quant_system.toss_live_read.reporting import (
    AtomicJsonTossLiveReadReportWriter,
)
from world_quant_system.toss_live_read.service import (
    DeterministicTossLiveReadCertifier,
)
from world_quant_system.toss_live_read.simulation import FakeTransport


def test_policy_example_loads() -> None:
    policy = load_policy("examples/toss_live_read_policy.example.json")
    assert policy.broker_writes_enabled is False


def test_atomic_report_contains_no_fixture_secrets(tmp_path: Path) -> None:
    now = datetime(2026, 8, 3, tzinfo=UTC)
    report = DeterministicTossLiveReadCertifier(
        policy=load_policy("examples/toss_live_read_policy.example.json"),
        transport=FakeTransport(),
        sleeper=lambda _: None,
    ).certify(
        material=FakeSecretProvider(
            client_id="fixture-client",
            client_secret="fixture-secret",
            account_id="1",
            credential_version="fixture-v1",
        ).load("toss"),
        now=now,
        kill_switch=KillSwitchState(
            mode=KillSwitchMode.NORMAL,
            reason="normal",
            activated_at=None,
        ),
    )
    output = tmp_path / "report.json"
    AtomicJsonTossLiveReadReportWriter(output).write(report)
    text = output.read_text(encoding="utf-8")
    json.loads(text)
    for forbidden in (
        "fixture-client",
        "fixture-secret",
        "fixture-live-token-not-real",
        "REDACTED-FIXTURE",
    ):
        assert forbidden not in text
