from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from world_quant_system.research.models import (
    ExperimentOutcome,
    ExperimentSpec,
    ExperimentStatus,
    HoldoutConsumedError,
    HoldoutConsumption,
    ParameterSearchAudit,
    ResearchSplit,
    ResearchWindow,
    canonical_json_object,
)
from world_quant_system.research.registry import SQLiteExperimentRegistry


@dataclass(frozen=True, slots=True)
class ResearchValiditySimulationResult:
    deterministic_digest_match: bool
    idempotent_registration: bool
    failed_outcome_retained: bool
    holdout_reuse_blocked: bool
    experiment_count: int


async def run_research_validity_simulation() -> ResearchValiditySimulationResult:
    with tempfile.TemporaryDirectory() as directory:
        registry = SQLiteExperimentRegistry(Path(directory) / "research")
        start = datetime(2020, 1, 1, tzinfo=UTC)
        spec = ExperimentSpec(
            strategy_name="sma-cross",
            strategy_version="1.0.0",
            parameters_json=canonical_json_object(
                {"long_window": 100, "short_window": 20}
            ),
            dataset_digest="a" * 64,
            code_commit="98a9c98f0e321b72dcdab22d7d4fe9f7fac31257",
            cost_model_json=canonical_json_object({"commission_bps": "15"}),
            execution_model_json=canonical_json_object({"fill": "next_open"}),
            split=ResearchSplit(
                train=ResearchWindow(start, start + timedelta(days=365)),
                validation=ResearchWindow(
                    start + timedelta(days=365),
                    start + timedelta(days=545),
                ),
                holdout=ResearchWindow(
                    start + timedelta(days=545),
                    start + timedelta(days=725),
                ),
            ),
            search_audit=ParameterSearchAudit(
                search_id="simulation",
                search_space_json=canonical_json_object(
                    {"long_window": [100], "short_window": [20]}
                ),
                trial_number=1,
                total_trials=1,
                selection_metric="validation_sharpe",
            ),
        )
        first = await registry.register(spec, registered_at=start)
        second = await registry.register(
            spec,
            registered_at=start + timedelta(days=1),
        )
        failed = ExperimentOutcome(
            experiment_id=first.experiment_id,
            status=ExperimentStatus.FAILED,
            completed_at=start + timedelta(days=726),
            failure_reason="robustness gate failed",
        )
        await registry.record_outcome(failed)
        consumption = HoldoutConsumption(
            experiment_id=first.experiment_id,
            result_digest="b" * 64,
            consumed_at=start + timedelta(days=727),
        )
        await registry.consume_holdout(consumption)
        reuse_blocked = False
        try:
            await registry.consume_holdout(consumption)
        except HoldoutConsumedError:
            reuse_blocked = True
        snapshot = await registry.get(first.experiment_id)
        experiments = await registry.query()
        return ResearchValiditySimulationResult(
            deterministic_digest_match=(first.research_digest == spec.research_digest),
            idempotent_registration=(first == second),
            failed_outcome_retained=(snapshot.outcome == failed),
            holdout_reuse_blocked=reuse_blocked,
            experiment_count=len(experiments),
        )


def main() -> None:
    result = asyncio.run(run_research_validity_simulation())
    print(f"Research experiments: {result.experiment_count}")
    print(
        "Deterministic research digest: "
        f"{'match' if result.deterministic_digest_match else 'mismatch'}"
    )
    print(
        "Idempotent registration: "
        f"{'yes' if result.idempotent_registration else 'no'}"
    )
    print(
        "Failed outcomes retained: "
        f"{'yes' if result.failed_outcome_retained else 'no'}"
    )
    print(
        "Holdout reuse blocked: "
        f"{'yes' if result.holdout_reuse_blocked else 'no'}"
    )
    if not all(
        (
            result.deterministic_digest_match,
            result.idempotent_registration,
            result.failed_outcome_retained,
            result.holdout_reuse_blocked,
            result.experiment_count == 1,
        )
    ):
        raise RuntimeError("Research-validity simulation failed.")


if __name__ == "__main__":
    main()
