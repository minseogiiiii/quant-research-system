from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from world_quant_system.research import (
    DataAvailabilityRecord,
    DelistingReason,
    DelistingRecord,
    ExperimentSpec,
    ParameterSearchAudit,
    PointInTimeBacktestContext,
    PointInTimeConfigurationError,
    PointInTimeDataKind,
    PointInTimeMember,
    PointInTimePolicy,
    PointInTimeSnapshot,
    ResearchConfigurationError,
    ResearchSplit,
    ResearchWindow,
    SecurityLifecycle,
    SQLiteExperimentRegistry,
    UniverseMembership,
    canonical_json_object,
    point_in_time_policy_json,
)

START = datetime(2020, 1, 1, tzinfo=UTC)


def lifecycle() -> SecurityLifecycle:
    return SecurityLifecycle(
        exchange="xkrx",
        symbol="005930",
        listed_at=datetime(1975, 6, 11, tzinfo=UTC),
        tradable_from=datetime(1975, 6, 11, tzinfo=UTC),
        source="test",
        source_digest="a" * 64,
    )


def membership() -> UniverseMembership:
    return UniverseMembership(
        universe_id="KOSPI",
        exchange="xkrx",
        symbol="005930",
        member_from=START,
        member_until=datetime(2025, 1, 1, tzinfo=UTC),
        available_at=START,
        source="test",
        source_digest="b" * 64,
    )


def experiment_spec(**overrides: object) -> ExperimentSpec:
    values: dict[str, object] = {
        "strategy_name": "sma-cross",
        "strategy_version": "1.0.0",
        "parameters_json": canonical_json_object(
            {"long_window": 100, "short_window": 20}
        ),
        "dataset_digest": "c" * 64,
        "code_commit": "98a9c98f0e321b72dcdab22d7d4fe9f7fac31257",
        "cost_model_json": canonical_json_object({"commission_bps": "15"}),
        "execution_model_json": canonical_json_object({"fill": "next_open"}),
        "split": ResearchSplit(
            train=ResearchWindow(START, START + timedelta(days=365)),
            validation=ResearchWindow(
                START + timedelta(days=365),
                START + timedelta(days=545),
            ),
            holdout=ResearchWindow(
                START + timedelta(days=545),
                START + timedelta(days=725),
            ),
        ),
        "search_audit": ParameterSearchAudit(
            search_id="standalone",
            search_space_json="{}",
            trial_number=1,
            total_trials=1,
            selection_metric="not_applicable",
        ),
    }
    values.update(overrides)
    return ExperimentSpec(**values)  # type: ignore[arg-type]


def test_security_lifecycle_normalizes_codes_and_uses_deterministic_id() -> None:
    first = lifecycle()
    second = lifecycle()

    assert first.exchange == "XKRX"
    assert first.symbol == "005930"
    assert first.lifecycle_id == second.lifecycle_id
    assert first.is_listed_at(START)
    assert first.is_tradable_at(START)


def test_security_lifecycle_rejects_invalid_time_ordering() -> None:
    with pytest.raises(PointInTimeConfigurationError):
        SecurityLifecycle(
            exchange="XKRX",
            symbol="BAD",
            listed_at=START,
            tradable_from=START - timedelta(days=1),
            source="test",
            source_digest="a" * 64,
        )

    with pytest.raises(PointInTimeConfigurationError):
        SecurityLifecycle(
            exchange="XKRX",
            symbol="BAD",
            listed_at=START,
            tradable_from=START,
            delisted_at=START,
            source="test",
            source_digest="a" * 64,
        )


def test_membership_and_availability_preserve_effective_and_known_times() -> None:
    candidate = membership()
    availability = DataAvailabilityRecord(
        data_id="candle-id",
        data_kind=PointInTimeDataKind.CANDLE,
        exchange="XKRX",
        symbol="005930",
        effective_at=START + timedelta(days=1),
        available_at=START + timedelta(days=2),
        source="test",
        source_digest="d" * 64,
    )

    assert candidate.is_effective_at(START + timedelta(days=1))
    assert candidate.is_known_at(START)
    assert not availability.is_available_at(START + timedelta(days=1))
    assert availability.is_available_at(START + timedelta(days=2))


def test_candle_availability_cannot_precede_effective_timestamp() -> None:
    with pytest.raises(PointInTimeConfigurationError):
        DataAvailabilityRecord(
            data_id="future-candle",
            data_kind=PointInTimeDataKind.CANDLE,
            exchange="XKRX",
            symbol="005930",
            effective_at=START + timedelta(days=1),
            available_at=START,
            source="test",
            source_digest="d" * 64,
        )

def test_delisting_requires_last_tradable_before_delisted_at() -> None:
    with pytest.raises(PointInTimeConfigurationError):
        DelistingRecord(
            exchange="XKRX",
            symbol="000001",
            last_tradable_at=START,
            delisted_at=START,
            available_at=START,
            reason=DelistingReason.UNKNOWN,
            source="test",
            source_digest="e" * 64,
        )


def test_snapshot_and_context_digests_are_order_independent_at_construction() -> None:
    first_lifecycle = lifecycle()
    first_membership = membership()
    member = PointInTimeMember(
        exchange="XKRX",
        symbol="005930",
        lifecycle_id=first_lifecycle.lifecycle_id,
        membership_id=first_membership.membership_id,
    )
    snapshot = PointInTimeSnapshot(
        universe_id="KOSPI",
        as_of=START,
        policy=PointInTimePolicy(),
        members=(member,),
    )
    context = PointInTimeBacktestContext(
        universe_id="KOSPI",
        exchange="XKRX",
        symbol="005930",
        start=START,
        end=START + timedelta(days=2),
        policy=PointInTimePolicy(),
        lifecycle_id=first_lifecycle.lifecycle_id,
        membership_ids=(first_membership.membership_id,),
        availability_ids=(
            DataAvailabilityRecord(
                data_id="candle-id",
                data_kind=PointInTimeDataKind.CANDLE,
                exchange="XKRX",
                symbol="005930",
                effective_at=START + timedelta(days=1),
                available_at=START + timedelta(days=1),
                source="test",
                source_digest="f" * 64,
            ).availability_id,
        ),
    )

    assert len(snapshot.universe_digest) == 64
    assert len(snapshot.snapshot_digest) == 64
    assert len(context.context_digest) == 64
    assert point_in_time_policy_json(context.policy) == (
        '{"as_of_policy":"effective_and_available",'
        '"availability_policy":"require_record",'
        '"delisting_policy":"require_record"}'
    )


def test_experiment_spec_preserves_legacy_digest_shape_when_context_absent() -> None:
    baseline = experiment_spec()
    with_context = experiment_spec(
        point_in_time_context_digest="f" * 64,
        point_in_time_policy_json=point_in_time_policy_json(PointInTimePolicy()),
    )

    assert "point_in_time" not in baseline.to_document()
    assert "point_in_time" in with_context.to_document()
    assert baseline.research_digest != with_context.research_digest


def test_experiment_spec_requires_context_digest_and_policy_together() -> None:
    with pytest.raises(ResearchConfigurationError):
        experiment_spec(point_in_time_context_digest="f" * 64)
    with pytest.raises(ResearchConfigurationError):
        experiment_spec(
            point_in_time_policy_json=point_in_time_policy_json(
                PointInTimePolicy()
            )
        )


@pytest.mark.asyncio
async def test_registry_round_trips_point_in_time_experiment_context(
    tmp_path: Path,
) -> None:
    candidate = experiment_spec(
        point_in_time_context_digest="f" * 64,
        point_in_time_policy_json=point_in_time_policy_json(PointInTimePolicy()),
    )
    registry = SQLiteExperimentRegistry(tmp_path / "research")

    record = await registry.register(candidate, registered_at=START)
    loaded = await registry.get(record.experiment_id)

    assert loaded.record.spec == candidate
    assert loaded.record.research_digest == candidate.research_digest
