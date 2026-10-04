from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from world_quant_system.cli import main
from world_quant_system.data import (
    DataQualityReport,
    QualityDatasetKind,
    QualityStatus,
    SQLiteNormalizedMarketDataStore,
)
from world_quant_system.domain import Candle, CandleInterval, CandlePage


def test_robustness_cli_runs_networkless_and_writes_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    normalized_root = tmp_path / "normalized"
    asyncio.run(_seed_store(normalized_root))
    output = tmp_path / "robustness.json"

    main(
        [
            "robustness",
            "--normalized-root",
            str(normalized_root),
            "--symbol",
            "ABC",
            "--historical-dataset-digest",
            "a" * 64,
            "--strategy",
            "buy-and-hold",
            "--fold",
            (
                "2024-01-01T00:00:00Z,2024-02-10T00:00:00Z,"
                "2024-02-10T00:00:00Z,2024-03-01T00:00:00Z,"
                "2024-03-01T00:00:00Z,2024-03-21T00:00:00Z"
            ),
            "--cost-multiplier",
            "1",
            "--cost-multiplier",
            "2",
            "--execution-delay",
            "1",
            "--execution-delay",
            "2",
            "--minimum-observations",
            "10",
            "--minimum-test-return",
            "-1",
            "--maximum-drawdown",
            "-1",
            "--maximum-return-degradation",
            "-2",
            "--required-pass-rate",
            "0",
            "--commission-bps",
            "0",
            "--slippage-bps",
            "0",
            "--max-volume-participation",
            "1",
            "--json-output",
            str(output),
        ]
    )

    text = capsys.readouterr().out
    assert "Execution mode: ROBUSTNESS_RESEARCH" in text
    assert "Network access: DISABLED" in text
    assert "Live trading: DISABLED" in text
    assert "Order submission: DISABLED" in text
    document = json.loads(output.read_text(encoding="utf-8"))
    assert len(document["cases"]) == 4
    assert document["dataset_digest"] == "a" * 64
    assert document["passed"] is True


def test_robustness_cli_rejects_invalid_fold(tmp_path: Path) -> None:
    root = tmp_path / "normalized"
    root.mkdir()
    (root / "normalized.sqlite3").touch()

    with pytest.raises(SystemExit) as error:
        main(
            [
                "robustness",
                "--normalized-root",
                str(root),
                "--symbol",
                "ABC",
                "--historical-dataset-digest",
                "a" * 64,
                "--fold",
                "2024-01-01,2024-02-01",
            ]
        )
    assert error.value.code == 2


async def _seed_store(root: Path) -> None:
    store = SQLiteNormalizedMarketDataStore(root)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    candles = tuple(
        Candle(
            symbol="ABC",
            interval=CandleInterval.DAY_1,
            timestamp=start + timedelta(days=index),
            open_price=Decimal("100") + index,
            high_price=Decimal("101") + index,
            low_price=Decimal("99") + index,
            close_price=Decimal("100.5") + index,
            volume=1_000_000,
            currency="USD",
            source="test",
        )
        for index in range(100)
    )
    report = DataQualityReport(
        report_id=str(uuid4()),
        assessment_key=hashlib.sha256(b"robustness-assessment").hexdigest(),
        record_id=str(uuid4()),
        raw_content_sha256=hashlib.sha256(b"robustness-raw").hexdigest(),
        dataset_kind=QualityDatasetKind.CANDLES,
        status=QualityStatus.PASS,
        checked_at=start + timedelta(days=101),
        validator_version="1.0.0",
        policy_fingerprint=hashlib.sha256(b"robustness-policy").hexdigest(),
        item_count=len(candles),
        issues=(),
    )
    await store.save_candle_page(
        CandlePage(candles, None),
        report,
        normalized_at=start + timedelta(days=101),
        normalizer_version="1.0.0",
    )
