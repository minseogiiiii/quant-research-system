from datetime import UTC, datetime, timedelta, timezone

import pytest

from world_quant_system.research import (
    ExperimentOutcome,
    ExperimentSpec,
    ExperimentStatus,
    ParameterSearchAudit,
    ResearchConfigurationError,
    ResearchSplit,
    ResearchWindow,
    canonical_json_object,
)

START = datetime(2020, 1, 1, tzinfo=UTC)


def split() -> ResearchSplit:
    return ResearchSplit(
        train=ResearchWindow(START, START + timedelta(days=365)),
        validation=ResearchWindow(
            START + timedelta(days=365),
            START + timedelta(days=545),
        ),
        holdout=ResearchWindow(
            START + timedelta(days=545),
            START + timedelta(days=725),
        ),
    )


def audit() -> ParameterSearchAudit:
    return ParameterSearchAudit(
        search_id="sma-grid-v1",
        search_space_json=canonical_json_object(
            {"long_window": [80, 100], "short_window": [10, 20]}
        ),
        trial_number=1,
        total_trials=4,
        selection_metric="validation_sharpe",
    )


def spec(**overrides: object) -> ExperimentSpec:
    values: dict[str, object] = {
        "strategy_name": "sma-cross",
        "strategy_version": "1.0.0",
        "parameters_json": canonical_json_object(
            {"long_window": 100, "short_window": 20}
        ),
        "dataset_digest": "a" * 64,
        "code_commit": "98a9c98f0e321b72dcdab22d7d4fe9f7fac31257",
        "cost_model_json": canonical_json_object(
            {"commission_bps": "15", "slippage_bps": "10"}
        ),
        "execution_model_json": canonical_json_object(
            {"fill": "next_open", "max_volume_participation": "0.10"}
        ),
        "split": split(),
        "search_audit": audit(),
    }
    values.update(overrides)
    return ExperimentSpec(**values)  # type: ignore[arg-type]


def test_research_split_uses_non_overlapping_half_open_windows() -> None:
    candidate = split()

    assert candidate.train.end == candidate.validation.start
    assert candidate.validation.end == candidate.holdout.start
    assert len(candidate.fingerprint) == 64


def test_research_split_rejects_overlap_and_naive_timestamps() -> None:
    with pytest.raises(ResearchConfigurationError):
        ResearchWindow(datetime(2020, 1, 1), datetime(2020, 2, 1))

    with pytest.raises(ResearchConfigurationError):
        ResearchSplit(
            train=ResearchWindow(START, START + timedelta(days=10)),
            validation=ResearchWindow(
                START + timedelta(days=9),
                START + timedelta(days=20),
            ),
            holdout=ResearchWindow(
                START + timedelta(days=20),
                START + timedelta(days=30),
            ),
        )


def test_semantically_identical_timezones_and_json_produce_same_digest() -> None:
    first = spec()
    eastern = timezone(timedelta(hours=-5))
    shifted = ResearchSplit(
        train=ResearchWindow(
            first.split.train.start.astimezone(eastern),
            first.split.train.end.astimezone(eastern),
        ),
        validation=ResearchWindow(
            first.split.validation.start.astimezone(eastern),
            first.split.validation.end.astimezone(eastern),
        ),
        holdout=ResearchWindow(
            first.split.holdout.start.astimezone(eastern),
            first.split.holdout.end.astimezone(eastern),
        ),
    )
    second = spec(
        parameters_json=canonical_json_object(
            {"short_window": 20, "long_window": 100}
        ),
        split=shifted,
    )

    assert first.research_digest == second.research_digest
    assert first.experiment_id == second.experiment_id


def test_research_digest_changes_with_cost_data_code_or_split() -> None:
    baseline = spec()

    assert spec(dataset_digest="b" * 64).research_digest != baseline.research_digest
    assert spec(code_commit="a" * 40).research_digest != baseline.research_digest
    assert (
        spec(
            cost_model_json=canonical_json_object({"commission_bps": "30"})
        ).research_digest
        != baseline.research_digest
    )


def test_parent_requires_change_reason() -> None:
    parent_id = spec().experiment_id

    with pytest.raises(ResearchConfigurationError):
        spec(parent_experiment_id=parent_id)
    with pytest.raises(ResearchConfigurationError):
        spec(change_reason="changed windows")


def test_search_audit_rejects_invalid_trial_number() -> None:
    with pytest.raises(ResearchConfigurationError):
        ParameterSearchAudit(
            search_id="grid",
            search_space_json="{}",
            trial_number=3,
            total_trials=2,
            selection_metric="sharpe",
        )


def test_outcome_enforces_success_and_failure_shapes() -> None:
    experiment_id = spec().experiment_id
    now = datetime(2026, 7, 29, tzinfo=UTC)
    success = ExperimentOutcome(
        experiment_id=experiment_id,
        status=ExperimentStatus.SUCCEEDED,
        completed_at=now,
        result_digest="c" * 64,
    )
    failure = ExperimentOutcome(
        experiment_id=experiment_id,
        status=ExperimentStatus.FAILED,
        completed_at=now,
        failure_reason="data coverage gate failed",
    )

    assert success.result_digest == "c" * 64
    assert failure.failure_reason == "data coverage gate failed"
    with pytest.raises(ResearchConfigurationError):
        ExperimentOutcome(
            experiment_id=experiment_id,
            status=ExperimentStatus.SUCCEEDED,
            completed_at=now,
        )


def test_corporate_action_context_is_pinned_in_research_digest() -> None:
    policy_json = canonical_json_object(
        {
            "fractional_share_policy": "reject",
            "require_explicit_delisting_value": True,
            "require_known_before_event": True,
        }
    )
    candidate = spec(
        corporate_action_context_digest="d" * 64,
        corporate_action_policy_json=policy_json,
        dividend_tax_model_digest="e" * 64,
    )

    assert candidate.to_document()["corporate_actions"] == {
        "context_digest": "d" * 64,
        "policy": {
            "fractional_share_policy": "reject",
            "require_explicit_delisting_value": True,
            "require_known_before_event": True,
        },
        "dividend_tax_model_digest": "e" * 64,
    }
    assert candidate.research_digest != spec().research_digest

    with pytest.raises(ResearchConfigurationError):
        spec(corporate_action_context_digest="d" * 64)
