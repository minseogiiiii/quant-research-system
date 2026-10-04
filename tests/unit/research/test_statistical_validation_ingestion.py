from __future__ import annotations

from pathlib import Path

import pytest

from world_quant_system.research.statistical_validation import (
    parse_statistical_returns_csv,
)
from world_quant_system.research.statistical_validation_models import (
    StatisticalValidationConfigurationError,
)


def test_parse_statistical_returns_csv(tmp_path: Path) -> None:
    source = tmp_path / "returns.csv"
    source.write_text(
        "timestamp,trial-a,trial-b\n"
        "2024-01-01T00:00:00Z,0.01,0.02\n"
        "2024-01-02T00:00:00Z,-0.01,0.01\n",
        encoding="utf-8",
    )

    matrix = parse_statistical_returns_csv(source)

    assert matrix.trial_ids == ("trial-a", "trial-b")
    assert matrix.observation_count == 2


def test_parser_rejects_duplicate_timestamp(tmp_path: Path) -> None:
    source = tmp_path / "returns.csv"
    source.write_text(
        "timestamp,trial-a,trial-b\n"
        "2024-01-01T00:00:00Z,0.01,0.02\n"
        "2024-01-01T00:00:00Z,-0.01,0.01\n",
        encoding="utf-8",
    )

    with pytest.raises(
        StatisticalValidationConfigurationError,
        match="strictly increasing",
    ):
        parse_statistical_returns_csv(source)
