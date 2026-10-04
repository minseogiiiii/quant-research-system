import json
from datetime import UTC, datetime
from pathlib import Path

from world_quant_system.research.historical_shadow import (
    DeterministicHistoricalShadowRunner,
)
from world_quant_system.research.historical_shadow_reporting import (
    AtomicJsonHistoricalShadowReportWriter,
)
from world_quant_system.research.historical_shadow_simulation import (
    build_synthetic_run,
)


def test_atomic_writer_persists_report(tmp_path: Path) -> None:
    manifest, matrix, policy = build_synthetic_run()
    report = DeterministicHistoricalShadowRunner(
        evidence_manifest=manifest,
        return_matrix=matrix,
        policy=policy,
    ).run(created_at=datetime(2026, 1, 1, tzinfo=UTC))
    output = tmp_path / "reports" / "shadow.json"

    AtomicJsonHistoricalShadowReportWriter(output).write(report)

    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["report_id"] == report.report_id
    assert document["report_digest"] == report.report_digest
    assert document["execution_mode"] == "historical_shadow"
    assert not tuple(output.parent.glob("*.tmp"))
