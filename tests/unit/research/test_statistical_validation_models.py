from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from world_quant_system.research.statistical_validation_models import (
    FailedStatisticalTrial,
    StatisticalReturnsMatrix,
    StatisticalValidationConfigurationError,
    StatisticalValidationPolicy,
)


def test_returns_matrix_is_deterministic_and_strict() -> None:
    matrix = _matrix()

    assert matrix.observation_count == 16
    assert matrix.successful_trial_count == 2
    assert matrix.matrix_digest == _matrix().matrix_digest


def test_returns_matrix_rejects_time_reversal() -> None:
    matrix = _matrix()

    with pytest.raises(
        StatisticalValidationConfigurationError,
        match="strictly increasing",
    ):
        StatisticalReturnsMatrix(
            timestamps=tuple(reversed(matrix.timestamps)),
            trial_ids=matrix.trial_ids,
            returns_by_trial=matrix.returns_by_trial,
        )


def test_policy_rejects_odd_cscv_partition_count() -> None:
    with pytest.raises(
        StatisticalValidationConfigurationError,
        match="even integer",
    ):
        StatisticalValidationPolicy(cscv_partitions=7)


def test_failed_trial_requires_reason() -> None:
    with pytest.raises(
        StatisticalValidationConfigurationError,
        match="nonblank",
    ):
        FailedStatisticalTrial(trial_id="failed", failure_reason="")


def _matrix() -> StatisticalReturnsMatrix:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    timestamps = tuple(start + timedelta(days=index) for index in range(16))
    first = tuple(
        Decimal(format(0.001 + 0.002 * math.sin(index), ".12f"))
        for index in range(16)
    )
    second = tuple(
        Decimal(format(0.0005 + 0.002 * math.cos(index), ".12f"))
        for index in range(16)
    )
    return StatisticalReturnsMatrix(
        timestamps=timestamps,
        trial_ids=("first", "second"),
        returns_by_trial=(first, second),
    )
