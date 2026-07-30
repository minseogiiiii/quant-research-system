from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from world_quant_system.research.statistical_validation import (
    DeterministicStatisticalValidator,
)
from world_quant_system.research.statistical_validation_models import (
    StatisticalReturnsMatrix,
    StatisticalValidationPolicy,
)
from world_quant_system.research.statistical_validation_reporting import (
    AtomicJsonStatisticalValidationReportWriter,
)


def test_atomic_statistical_writer(tmp_path: Path) -> None:
    report = DeterministicStatisticalValidator(
        matrix=_matrix(),
        dataset_digest="d" * 64,
        search_id="writer",
        attempted_trial_count=2,
        policy=StatisticalValidationPolicy(
            minimum_observations=32,
            cscv_partitions=8,
            minimum_deflated_sharpe_probability=Decimal("0.01"),
            maximum_probability_backtest_overfitting=Decimal("0.99"),
            minimum_rank_correlation=-1.0,
            require_bonferroni_pass=False,
            require_false_discovery_pass=False,
        ),
    ).run(created_at=datetime(2026, 1, 1, tzinfo=UTC))
    output = tmp_path / "statistical.json"

    AtomicJsonStatisticalValidationReportWriter(output).write(report)

    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["report_id"] == report.report_id
    assert document["report_digest"] == report.report_digest
    assert not tuple(tmp_path.glob("*.tmp"))


def _matrix() -> StatisticalReturnsMatrix:
    observation_count = 64
    start = datetime(2024, 1, 1, tzinfo=UTC)
    timestamps = tuple(
        start + timedelta(days=index) for index in range(observation_count)
    )
    first = tuple(
        Decimal(format(0.001 + 0.002 * math.sin(index / 3), ".12f"))
        for index in range(observation_count)
    )
    second = tuple(
        Decimal(format(0.0002 + 0.002 * math.cos(index / 4), ".12f"))
        for index in range(observation_count)
    )
    return StatisticalReturnsMatrix(
        timestamps=timestamps,
        trial_ids=("first", "second"),
        returns_by_trial=(first, second),
    )
