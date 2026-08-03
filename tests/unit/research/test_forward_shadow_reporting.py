import json
from pathlib import Path

import pytest

from world_quant_system.research.forward_shadow import (
    DeterministicForwardShadowRunner,
    FixedForwardShadowClock,
)
from world_quant_system.research.forward_shadow_models import (
    ForwardShadowIntegrityError,
)
from world_quant_system.research.forward_shadow_reporting import (
    AtomicJsonForwardShadowReportWriter,
    AtomicJsonForwardShadowStateStore,
)
from world_quant_system.research.forward_shadow_simulation import (
    build_synthetic_forward_shadow,
)


def test_state_store_round_trip(tmp_path: Path) -> None:
    manifest, policy, observations, now = build_synthetic_forward_shadow()
    report = DeterministicForwardShadowRunner(
        evidence_manifest=manifest,
        policy=policy,
        clock=FixedForwardShadowClock(now),
    ).process(observations[:4])
    path = tmp_path / "state.json"
    store = AtomicJsonForwardShadowStateStore(path)

    store.save(report.state)
    loaded = store.load()

    assert loaded is not None
    assert loaded.state_digest == report.state.state_digest


def test_state_store_rejects_tampering(tmp_path: Path) -> None:
    manifest, policy, observations, now = build_synthetic_forward_shadow()
    report = DeterministicForwardShadowRunner(
        evidence_manifest=manifest,
        policy=policy,
        clock=FixedForwardShadowClock(now),
    ).process(observations[:2])
    path = tmp_path / "state.json"
    store = AtomicJsonForwardShadowStateStore(path)
    store.save(report.state)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["equity"] = "999"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ForwardShadowIntegrityError, match="digest"):
        store.load()


def test_report_writer_persists_report(tmp_path: Path) -> None:
    manifest, policy, observations, now = build_synthetic_forward_shadow()
    report = DeterministicForwardShadowRunner(
        evidence_manifest=manifest,
        policy=policy,
        clock=FixedForwardShadowClock(now),
    ).process(observations[:3])
    path = tmp_path / "report.json"

    AtomicJsonForwardShadowReportWriter(path).write(report)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["report_digest"] == report.report_digest
