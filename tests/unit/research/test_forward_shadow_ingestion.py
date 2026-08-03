import json
from pathlib import Path

import pytest

from world_quant_system.research.forward_shadow import (
    load_forward_shadow_observations,
)
from world_quant_system.research.forward_shadow_models import (
    ForwardShadowConfigurationError,
)


def test_jsonl_loader_reads_sorted_returns(tmp_path: Path) -> None:
    path = tmp_path / "observations.jsonl"
    path.write_text(
        json.dumps(
            {
                "sequence": 0,
                "observed_at": "2026-01-01T00:00:00Z",
                "received_at": "2026-01-01T00:00:01Z",
                "returns": {"trend": "0.01", "carry": "-0.01"},
                "source_digest": "a" * 64,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    observations = load_forward_shadow_observations(path)

    assert tuple(item.candidate_id for item in observations[0].returns) == (
        "carry",
        "trend",
    )


def test_jsonl_loader_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "observations.jsonl"
    path.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(ForwardShadowConfigurationError, match="valid JSON"):
        load_forward_shadow_observations(path)
