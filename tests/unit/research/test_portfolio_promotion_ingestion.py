from __future__ import annotations

import csv
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from world_quant_system.research.portfolio_promotion import (
    load_candidate_evidence_manifest,
    parse_candidate_returns_csv,
)
from world_quant_system.research.portfolio_promotion_models import (
    PortfolioPromotionIntegrityError,
)


def test_returns_parser_sorts_candidate_columns(tmp_path: Path) -> None:
    source = tmp_path / "returns.csv"
    with source.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("timestamp", "beta", "alpha"))
        writer.writerow(("2026-01-01T00:00:00Z", "0.02", "0.01"))

    matrix = parse_candidate_returns_csv(source)

    assert matrix.candidate_ids == ("alpha", "beta")
    assert matrix.series_for("alpha")[0] == Decimal("0.01")


def test_manifest_rejects_report_bound_to_another_dataset(tmp_path: Path) -> None:
    digest = "a" * 64
    _write_json(
        tmp_path / "backtest.json",
        {"historical_dataset_digest": "b" * 64},
    )
    _write_json(
        tmp_path / "robustness.json",
        {
            "historical_dataset_digest": digest,
            "report_digest": "c" * 64,
            "passed": True,
        },
    )
    _write_json(
        tmp_path / "statistical.json",
        {
            "dataset_digest": digest,
            "report_digest": "d" * 64,
            "passed": True,
        },
    )
    manifest = tmp_path / "manifest.json"
    _write_json(
        manifest,
        {
            "schema_version": 1,
            "dataset_digest": digest,
            "candidates": [
                {
                    "candidate_id": "alpha",
                    "strategy_name": "test",
                    "strategy_version": "1",
                    "backtest_report": "backtest.json",
                    "backtest_report_sha256": _sha256(tmp_path / "backtest.json"),
                    "robustness_report": "robustness.json",
                    "robustness_report_sha256": _sha256(
                        tmp_path / "robustness.json"
                    ),
                    "statistical_report": "statistical.json",
                    "statistical_report_sha256": _sha256(
                        tmp_path / "statistical.json"
                    ),
                    "declared_turnover": "2",
                    "declared_cost_to_gross_profit_ratio": "0.1",
                }
            ],
        },
    )

    with pytest.raises(PortfolioPromotionIntegrityError, match="not bound"):
        load_candidate_evidence_manifest(manifest)


def _write_json(path: Path, document: object) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_manifest_rejects_evidence_file_sha256_mismatch(tmp_path: Path) -> None:
    digest = "a" * 64
    backtest = tmp_path / "backtest.json"
    robustness = tmp_path / "robustness.json"
    statistical = tmp_path / "statistical.json"
    _write_json(backtest, {"historical_dataset_digest": digest})
    _write_json(
        robustness,
        {
            "historical_dataset_digest": digest,
            "report_digest": "c" * 64,
            "passed": True,
        },
    )
    _write_json(
        statistical,
        {
            "dataset_digest": digest,
            "report_digest": "d" * 64,
            "passed": True,
        },
    )
    manifest = tmp_path / "manifest.json"
    _write_json(
        manifest,
        {
            "schema_version": 1,
            "dataset_digest": digest,
            "candidates": [
                {
                    "candidate_id": "alpha",
                    "strategy_name": "test",
                    "strategy_version": "1",
                    "backtest_report": backtest.name,
                    "backtest_report_sha256": "0" * 64,
                    "robustness_report": robustness.name,
                    "robustness_report_sha256": _sha256(robustness),
                    "statistical_report": statistical.name,
                    "statistical_report_sha256": _sha256(statistical),
                    "declared_turnover": "2",
                    "declared_cost_to_gross_profit_ratio": "0.1",
                }
            ],
        },
    )

    with pytest.raises(PortfolioPromotionIntegrityError, match="SHA-256 mismatch"):
        load_candidate_evidence_manifest(manifest)


def test_manifest_derives_turnover_and_cost_ratio_from_backtest(
    tmp_path: Path,
) -> None:
    digest = "a" * 64
    backtest = tmp_path / "backtest.json"
    robustness = tmp_path / "robustness.json"
    statistical = tmp_path / "statistical.json"
    _write_json(
        backtest,
        {
            "historical_dataset_digest": digest,
            "metrics": {
                "initial_equity": "100",
                "final_equity": "109",
                "turnover": "4.5",
                "commission_cost": "0.6",
                "slippage_cost": "0.4",
            },
        },
    )
    _write_json(
        robustness,
        {
            "historical_dataset_digest": digest,
            "report_digest": "c" * 64,
            "passed": True,
        },
    )
    _write_json(
        statistical,
        {
            "dataset_digest": digest,
            "report_digest": "d" * 64,
            "passed": True,
        },
    )
    manifest = tmp_path / "manifest.json"
    _write_json(
        manifest,
        {
            "schema_version": 1,
            "dataset_digest": digest,
            "candidates": [
                {
                    "candidate_id": "alpha",
                    "strategy_name": "test",
                    "strategy_version": "1",
                    "backtest_report": backtest.name,
                    "backtest_report_sha256": _sha256(backtest),
                    "robustness_report": robustness.name,
                    "robustness_report_sha256": _sha256(robustness),
                    "statistical_report": statistical.name,
                    "statistical_report_sha256": _sha256(statistical),
                }
            ],
        },
    )

    evidence = load_candidate_evidence_manifest(manifest)[0]

    assert evidence.declared_turnover == Decimal("4.5")
    assert evidence.declared_cost_to_gross_profit_ratio == Decimal("0.1")
