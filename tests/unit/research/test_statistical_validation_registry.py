from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from world_quant_system.research import (
    ExperimentOutcome,
    ExperimentSpec,
    ExperimentStatus,
    ParameterSearchAudit,
    ResearchSplit,
    ResearchWindow,
    SQLiteExperimentRegistry,
    canonical_json_object,
)
from world_quant_system.research.statistical_validation import build_registry_audit

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_registry_audit_preserves_failures_and_requires_completion(
    tmp_path: Path,
) -> None:
    registry = SQLiteExperimentRegistry(tmp_path / "registry")
    first = await registry.register(_spec(1), registered_at=NOW)
    second = await registry.register(_spec(2), registered_at=NOW)
    await registry.record_outcome(
        ExperimentOutcome(
            experiment_id=first.experiment_id,
            status=ExperimentStatus.SUCCEEDED,
            completed_at=NOW + timedelta(minutes=1),
            result_digest="b" * 64,
        )
    )
    await registry.record_outcome(
        ExperimentOutcome(
            experiment_id=second.experiment_id,
            status=ExperimentStatus.FAILED,
            completed_at=NOW + timedelta(minutes=2),
            failure_reason="robustness gate failed",
        )
    )

    audit, failed = await build_registry_audit(
        registry=registry,
        search_id="grid-v1",
        matrix_trial_ids=(first.experiment_id,),
    )

    assert audit.complete is True
    assert audit.succeeded_count == 1
    assert audit.failed_count == 1
    assert failed[0].experiment_id == second.experiment_id
    assert failed[0].failure_reason == "robustness gate failed"


def _spec(trial_number: int) -> ExperimentSpec:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    return ExperimentSpec(
        strategy_name="sma-cross",
        strategy_version="1.0.0",
        parameters_json=canonical_json_object(
            {"long_window": 100, "short_window": 19 + trial_number}
        ),
        dataset_digest="a" * 64,
        code_commit="57dff0179ba8028e18f00c8ba74e86cdf6ad4c68",
        cost_model_json=canonical_json_object({"commission_bps": "15"}),
        execution_model_json=canonical_json_object({"fill": "next_open"}),
        split=ResearchSplit(
            train=ResearchWindow(start, start + timedelta(days=100)),
            validation=ResearchWindow(
                start + timedelta(days=100),
                start + timedelta(days=150),
            ),
            holdout=ResearchWindow(
                start + timedelta(days=150),
                start + timedelta(days=200),
            ),
        ),
        search_audit=ParameterSearchAudit(
            search_id="grid-v1",
            search_space_json=canonical_json_object(
                {"short_window": [20, 21]}
            ),
            trial_number=trial_number,
            total_trials=2,
            selection_metric="validation_sharpe",
        ),
    )
