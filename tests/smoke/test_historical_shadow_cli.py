import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from world_quant_system.research.historical_shadow_cli import main
from world_quant_system.research.portfolio_promotion import (
    parse_candidate_returns_csv,
)


def test_historical_shadow_cli_runs_networkless(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    returns = tmp_path / "returns.csv"
    with returns.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("timestamp", "alpha", "beta", "gamma"))
        for index in range(70):
            writer.writerow(
                (
                    (start + timedelta(days=index)).isoformat(),
                    "0.001",
                    "0.0012",
                    "0.0008",
                )
            )
    matrix_digest = parse_candidate_returns_csv(returns).matrix_digest
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_digest": "d" * 64,
                "return_matrix_digest": matrix_digest,
                "events": [
                    {
                        "candidate_id": candidate_id,
                        "strategy_name": candidate_id,
                        "strategy_version": "1",
                        "state": "promoted",
                        "effective_at": start.isoformat(),
                        "available_at": start.isoformat(),
                        "evidence_digest": character * 64,
                        "reason": "smoke-test promotion",
                    }
                    for candidate_id, character in (
                        ("alpha", "a"),
                        ("beta", "b"),
                        ("gamma", "c"),
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "shadow.json"

    main(
        [
            "--evidence-manifest",
            str(manifest),
            "--returns-csv",
            str(returns),
            "--minimum-observations",
            "60",
            "--maximum-evidence-age-days",
            "365",
            "--maximum-candidate-weight",
            "0.40",
            "--transaction-cost-bps",
            "0",
            "--json-output",
            str(output),
        ]
    )

    console = capsys.readouterr().out
    assert "Execution mode: HISTORICAL_SHADOW" in console
    assert "Network access: DISABLED" in console
    assert "Broker provider: NONE" in console
    assert "Live trading: DISABLED" in console
    assert "Order submission: DISABLED" in console
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["execution_mode"] == "historical_shadow"
    assert document["order_submission"] == "disabled"
