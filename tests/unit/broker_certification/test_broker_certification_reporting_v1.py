from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from world_quant_system.broker_certification.ingestion import (
    load_capture_bundle,
    load_policy,
)
from world_quant_system.broker_certification.reporting import (
    AtomicJsonAdapterCertificationReportWriter,
)
from world_quant_system.broker_certification.service import (
    DeterministicReadOnlyAdapterCertifier,
)

ROOT = Path(__file__).resolve().parents[3]


def test_atomic_report_writer(tmp_path: Path) -> None:
    report = DeterministicReadOnlyAdapterCertifier().certify(
        policy=load_policy(
            ROOT / "examples/toss_read_only_certification_policy.example.json"
        ),
        capture=load_capture_bundle(
            ROOT / "examples/toss_read_only_capture_bundle.example.json"
        ),
        certified_at=datetime(2026, 8, 3, 21, 30, tzinfo=UTC),
    )
    output = tmp_path / "nested/certification.json"
    AtomicJsonAdapterCertificationReportWriter(output).write(report)
    stored = json.loads(output.read_text(encoding="utf-8"))
    assert stored["report_id"] == report.report_id
    assert stored["write_operations_enabled"] is False
    assert not list(output.parent.glob("*.tmp"))
