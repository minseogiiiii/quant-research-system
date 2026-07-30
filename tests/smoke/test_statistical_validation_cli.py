from __future__ import annotations

import csv
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from world_quant_system.cli import main


def test_statistical_validation_cli_runs_networkless(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "returns.csv"
    _write_returns(source)
    output = tmp_path / "report.json"

    main(
        [
            "statistical-validation",
            "--returns-csv",
            str(source),
            "--dataset-digest",
            "a" * 64,
            "--search-id",
            "cli-search",
            "--attempted-trials",
            "5",
            "--failed-trial",
            "failed=configuration rejected",
            "--minimum-observations",
            "80",
            "--cscv-partitions",
            "8",
            "--minimum-dsr-probability",
            "0.01",
            "--maximum-pbo",
            "0.99",
            "--minimum-rank-correlation",
            "-1",
            "--allow-without-bonferroni",
            "--allow-without-fdr",
            "--json-output",
            str(output),
        ]
    )

    text = capsys.readouterr().out
    assert "Execution mode: STATISTICAL_RESEARCH" in text
    assert "Network access: DISABLED" in text
    assert "Live trading: DISABLED" in text
    assert "Order submission: DISABLED" in text
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["search_id"] == "cli-search"
    assert document["attempted_trial_count"] == 5
    assert len(document["failed_trials"]) == 1


def test_statistical_validation_cli_requires_attempted_trials(
    tmp_path: Path,
) -> None:
    source = tmp_path / "returns.csv"
    _write_returns(source)

    with pytest.raises(SystemExit) as error:
        main(
            [
                "statistical-validation",
                "--returns-csv",
                str(source),
                "--dataset-digest",
                "a" * 64,
                "--search-id",
                "cli-search",
            ]
        )
    assert error.value.code == 2


def _write_returns(path: Path) -> None:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("timestamp", "trial-a", "trial-b", "trial-c", "trial-d"))
        for index in range(160):
            writer.writerow(
                (
                    (start + timedelta(days=index)).isoformat(),
                    0.0015 + 0.0015 * math.sin(index * 0.3),
                    0.0010 + 0.0015 * math.sin(index * 0.3 + 1),
                    0.0005 + 0.0015 * math.sin(index * 0.3 + 2),
                    0.0001 + 0.0015 * math.sin(index * 0.3 + 3),
                )
            )
