from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from world_quant_system.research.portfolio_promotion_cli import main


def test_portfolio_promotion_cli_runs_networkless(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset_digest = "a" * 64
    candidates = ("alpha-a", "alpha-b")
    manifest_candidates: list[dict[str, object]] = []
    for index, candidate_id in enumerate(candidates):
        backtest = tmp_path / f"{candidate_id}-backtest.json"
        robustness = tmp_path / f"{candidate_id}-robustness.json"
        statistical = tmp_path / f"{candidate_id}-statistical.json"
        _write_json(backtest, {"historical_dataset_digest": dataset_digest})
        _write_json(
            robustness,
            {
                "historical_dataset_digest": dataset_digest,
                "report_digest": ("b" if index == 0 else "c") * 64,
                "passed": True,
            },
        )
        _write_json(
            statistical,
            {
                "dataset_digest": dataset_digest,
                "report_digest": ("d" if index == 0 else "e") * 64,
                "passed": True,
            },
        )
        manifest_candidates.append(
            {
                "candidate_id": candidate_id,
                "strategy_name": "synthetic",
                "strategy_version": "1.0.0",
                "backtest_report": backtest.name,
                "backtest_report_sha256": _sha256(backtest),
                "robustness_report": robustness.name,
                "robustness_report_sha256": _sha256(robustness),
                "statistical_report": statistical.name,
                "statistical_report_sha256": _sha256(statistical),
                "declared_turnover": "4",
                "declared_cost_to_gross_profit_ratio": "0.10",
            }
        )
    manifest = tmp_path / "manifest.json"
    _write_json(
        manifest,
        {
            "schema_version": 1,
            "dataset_digest": dataset_digest,
            "candidates": manifest_candidates,
        },
    )
    returns = tmp_path / "returns.csv"
    start = datetime(2020, 1, 1, tzinfo=UTC)
    with returns.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("timestamp", *candidates))
        for index in range(100):
            writer.writerow(
                (
                    (start + timedelta(days=index)).isoformat(),
                    "0.002" if index % 4 else "-0.001",
                    "0.0018" if index % 5 else "-0.0008",
                )
            )
    output = tmp_path / "portfolio-report.json"

    main(
        [
            "--candidate-manifest",
            str(manifest),
            "--returns-csv",
            str(returns),
            "--maximum-severe-drawdown",
            "-0.90",
            "--minimum-effective-strategies",
            "1.0",
            "--json-output",
            str(output),
        ]
    )

    terminal = capsys.readouterr().out
    document = json.loads(output.read_text(encoding="utf-8"))
    assert "Network access: DISABLED" in terminal
    assert "Broker provider: NONE" in terminal
    assert document["live_trading"] == "DISABLED"
    assert document["order_submission"] == "DISABLED"


def _write_json(path: Path, document: object) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
