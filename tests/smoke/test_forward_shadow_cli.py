import json
from pathlib import Path

from world_quant_system.research.forward_shadow_cli import main
from world_quant_system.research.forward_shadow_simulation import (
    build_synthetic_forward_shadow,
)


def test_forward_shadow_cli_writes_checkpoint(
    tmp_path: Path,
    capsys: object,
) -> None:
    del capsys
    manifest, _, observations, now = build_synthetic_forward_shadow()
    manifest_path = tmp_path / "evidence.json"
    observations_path = tmp_path / "observations.jsonl"
    state_path = tmp_path / "state.json"
    manifest_path.write_text(
        json.dumps(manifest.to_document()),
        encoding="utf-8",
    )
    observations_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "sequence": item.sequence,
                    "observed_at": item.observed_at.isoformat(),
                    "received_at": item.received_at.isoformat(),
                    "returns": {
                        value.candidate_id: str(value.value)
                        for value in item.returns
                    },
                    "source_digest": item.source_digest,
                }
            )
            for item in observations[:3]
        )
        + "\n",
        encoding="utf-8",
    )

    main(
        [
            "--evidence-manifest",
            str(manifest_path),
            "--observations-jsonl",
            str(observations_path),
            "--state-file",
            str(state_path),
            "--rebalance-every-observations",
            "2",
            "--execution-delay-observations",
            "1",
            "--maximum-observation-lateness-seconds",
            "60",
            "--maximum-future-clock-skew-seconds",
            "0",
            "--now",
            now.isoformat(),
        ]
    )

    assert state_path.is_file()
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    assert raw["execution_mode"] == "forward_shadow"
    assert raw["order_submission"] == "disabled"
