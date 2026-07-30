import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from world_quant_system.research import (
    ExperimentOutcome,
    ExperimentSpec,
    ExperimentStatus,
    HoldoutConsumedError,
    HoldoutConsumption,
    ParameterSearchAudit,
    ResearchConflictError,
    ResearchIntegrityError,
    ResearchNotFoundError,
    ResearchSplit,
    ResearchWindow,
    SQLiteExperimentRegistry,
    canonical_json_object,
)

NOW = datetime(2026, 7, 29, 20, 0, tzinfo=UTC)


def experiment_spec(
    *,
    parent_experiment_id: str | None = None,
    change_reason: str | None = None,
    trial_number: int = 1,
) -> ExperimentSpec:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    return ExperimentSpec(
        strategy_name="sma-cross",
        strategy_version="1.0.0",
        parameters_json=canonical_json_object(
            {"long_window": 100, "short_window": 20 + trial_number - 1}
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
            search_id="sma-grid-v1",
            search_space_json=canonical_json_object(
                {"long_window": [100], "short_window": [20, 21]}
            ),
            trial_number=trial_number,
            total_trials=2,
            selection_metric="validation_sharpe",
        ),
        parent_experiment_id=parent_experiment_id,
        change_reason=change_reason,
    )


@pytest.mark.asyncio
async def test_registration_is_deterministic_and_idempotent_under_concurrency(
    tmp_path: Path,
) -> None:
    registry = SQLiteExperimentRegistry(tmp_path / "research")
    candidate = experiment_spec()

    results = await asyncio.gather(
        *(registry.register(candidate, registered_at=NOW) for _ in range(100))
    )

    assert all(result == results[0] for result in results)
    assert results[0].experiment_id == candidate.experiment_id
    assert await registry.query() == (
        await registry.get(candidate.experiment_id),
    )


@pytest.mark.asyncio
async def test_parent_experiment_must_exist_before_child(tmp_path: Path) -> None:
    registry = SQLiteExperimentRegistry(tmp_path / "research")
    missing_parent = experiment_spec().experiment_id
    child = experiment_spec(
        parent_experiment_id=missing_parent,
        change_reason="increase short window",
        trial_number=2,
    )

    with pytest.raises(ResearchNotFoundError):
        await registry.register(child, registered_at=NOW)

    parent = await registry.register(experiment_spec(), registered_at=NOW)
    child = experiment_spec(
        parent_experiment_id=parent.experiment_id,
        change_reason="increase short window",
        trial_number=2,
    )
    stored_child = await registry.register(
        child,
        registered_at=NOW + timedelta(minutes=1),
    )
    assert stored_child.spec.parent_experiment_id == parent.experiment_id


@pytest.mark.asyncio
async def test_outcomes_are_immutable_and_failures_are_preserved(
    tmp_path: Path,
) -> None:
    registry = SQLiteExperimentRegistry(tmp_path / "research")
    record = await registry.register(experiment_spec(), registered_at=NOW)
    failed = ExperimentOutcome(
        experiment_id=record.experiment_id,
        status=ExperimentStatus.FAILED,
        completed_at=NOW + timedelta(minutes=5),
        failure_reason="validation robustness gate failed",
    )

    assert await registry.record_outcome(failed) == failed
    retry = ExperimentOutcome(
        experiment_id=record.experiment_id,
        status=ExperimentStatus.FAILED,
        completed_at=NOW + timedelta(minutes=10),
        failure_reason="validation robustness gate failed",
    )
    assert await registry.record_outcome(retry) == failed
    snapshot = await registry.get(record.experiment_id)
    assert snapshot.outcome == failed

    with pytest.raises(ResearchConflictError):
        await registry.record_outcome(
            ExperimentOutcome(
                experiment_id=record.experiment_id,
                status=ExperimentStatus.SUCCEEDED,
                completed_at=NOW + timedelta(minutes=6),
                result_digest="b" * 64,
            )
        )


@pytest.mark.asyncio
async def test_holdout_is_consumed_exactly_once_even_under_concurrency(
    tmp_path: Path,
) -> None:
    registry = SQLiteExperimentRegistry(tmp_path / "research")
    record = await registry.register(experiment_spec(), registered_at=NOW)

    async def consume(index: int) -> HoldoutConsumption:
        return await registry.consume_holdout(
            HoldoutConsumption(
                experiment_id=record.experiment_id,
                result_digest=f"{index:064x}",
                consumed_at=NOW + timedelta(minutes=index),
            )
        )

    results = await asyncio.gather(
        *(consume(index) for index in range(1, 21)),
        return_exceptions=True,
    )

    successes = [item for item in results if isinstance(item, HoldoutConsumption)]
    failures = [item for item in results if isinstance(item, HoldoutConsumedError)]
    assert len(successes) == 1
    assert len(failures) == 19
    snapshot = await registry.get(record.experiment_id)
    assert snapshot.holdout_consumption == successes[0]


@pytest.mark.asyncio
async def test_catalog_tampering_is_detected(tmp_path: Path) -> None:
    registry = SQLiteExperimentRegistry(tmp_path / "research")
    record = await registry.register(experiment_spec(), registered_at=NOW)
    database = registry.root / "research.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE experiments SET spec_json = ? WHERE experiment_id = ?",
            ("{}", record.experiment_id),
        )

    with pytest.raises(ResearchIntegrityError):
        await registry.get(record.experiment_id)


def test_sqlite_connections_are_closed_after_each_registry_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TrackingConnection(sqlite3.Connection):
        is_closed = False

        def close(self) -> None:
            self.is_closed = True
            super().close()

    original_connect = sqlite3.connect
    opened_connections: list[TrackingConnection] = []

    def tracking_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["factory"] = TrackingConnection
        connection = cast(TrackingConnection, original_connect(*args, **kwargs))
        opened_connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    registry = SQLiteExperimentRegistry(tmp_path / "research")

    async def exercise() -> None:
        record = await registry.register(experiment_spec(), registered_at=NOW)
        await registry.get(record.experiment_id)
        await registry.query()
        await registry.record_outcome(
            ExperimentOutcome(
                experiment_id=record.experiment_id,
                status=ExperimentStatus.SUCCEEDED,
                completed_at=NOW + timedelta(minutes=1),
                result_digest="c" * 64,
            )
        )

    asyncio.run(exercise())

    assert opened_connections
    assert all(connection.is_closed for connection in opened_connections)
