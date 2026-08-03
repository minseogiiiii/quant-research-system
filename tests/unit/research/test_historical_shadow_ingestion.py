import json
from pathlib import Path

import pytest

from world_quant_system.research.historical_shadow import (
    load_shadow_evidence_manifest,
)
from world_quant_system.research.historical_shadow_models import (
    HistoricalShadowConfigurationError,
    ShadowEvidenceState,
)


def test_manifest_loader_parses_and_sorts_events(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_digest": "d" * 64,
                "return_matrix_digest": "e" * 64,
                "events": [
                    {
                        "candidate_id": "beta",
                        "strategy_name": "beta",
                        "strategy_version": "1",
                        "state": "promoted",
                        "effective_at": "2024-01-02T00:00:00Z",
                        "available_at": "2024-01-02T00:00:00Z",
                        "evidence_digest": "b" * 64,
                        "reason": "passed",
                    },
                    {
                        "candidate_id": "alpha",
                        "strategy_name": "alpha",
                        "strategy_version": "1",
                        "state": "promoted",
                        "effective_at": "2024-01-01T00:00:00Z",
                        "available_at": "2024-01-01T00:00:00Z",
                        "evidence_digest": "a" * 64,
                        "reason": "passed",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    manifest = load_shadow_evidence_manifest(path)

    assert tuple(item.candidate_id for item in manifest.events) == (
        "alpha",
        "beta",
    )
    assert manifest.events[0].state is ShadowEvidenceState.PROMOTED


def test_manifest_loader_rejects_unknown_schema(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "dataset_digest": "d" * 64,
                "return_matrix_digest": "e" * 64,
                "events": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        HistoricalShadowConfigurationError,
        match="schema_version",
    ):
        load_shadow_evidence_manifest(path)
