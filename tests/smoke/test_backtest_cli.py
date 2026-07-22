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


def test_backtest_cli_fails_closed_when_database_is_missing(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            [
                "backtest",
                "--normalized-root",
                str(tmp_path),
                "--strategy",
                "buy-and-hold",
                "--symbol",
                "ABC",
            ]
        )
    assert error.value.code == 2


def test_backtest_cli_runs_networkless_and_writes_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    normalized_root = tmp_path / "normalized"
    asyncio.run(_seed_normalized_store(normalized_root))
    output_path = tmp_path / "summary.json"
    main(
        [
            "backtest",
            "--normalized-root",
            str(normalized_root),
            "--strategy",
            "buy-and-hold",
            "--symbol",
            "ABC",
            "--initial-cash",
            "1200",
            "--commission-bps",
            "0",
            "--slippage-bps",
            "0",
            "--max-volume-participation",
            "1",
            "--json-output",
            str(output_path),
        ]
    )
    output = capsys.readouterr().out
    assert "Network access: DISABLED" in output
    assert "Live trading: DISABLED" in output
    assert "Order submission: DISABLED" in output
    summary = json.loads(output_path.read_text(encoding="utf-8"))
    assert summary["execution_mode"] == "replay"
    assert summary["live_trading"] == "disabled"
    assert summary["order_submission"] == "disabled"
    assert summary["event_count"] == 3
    assert summary["configuration"]["symbol"] == "ABC"
    assert summary["configuration"]["interval"] == "1d"
    assert summary["configuration"]["commission_bps"] == "0"
    assert summary["configuration"]["slippage_bps"] == "0"


async def _seed_normalized_store(root: Path) -> None:
    store = SQLiteNormalizedMarketDataStore(root)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles = tuple(
        Candle(
            symbol="ABC",
            interval=CandleInterval.DAY_1,
            timestamp=start + timedelta(days=index),
            open_price=price,
            high_price=price,
            low_price=price,
            close_price=price,
            volume=1_000_000,
            currency="USD",
            source="test",
        )
        for index, price in enumerate(
            (Decimal("10"), Decimal("12"), Decimal("15"))
        )
    )
    report = DataQualityReport(
        report_id=str(uuid4()),
        assessment_key=hashlib.sha256(b"assessment").hexdigest(),
        record_id=str(uuid4()),
        raw_content_sha256=hashlib.sha256(b"raw").hexdigest(),
        dataset_kind=QualityDatasetKind.CANDLES,
        status=QualityStatus.PASS,
        checked_at=start + timedelta(days=4),
        validator_version="1.0.0",
        policy_fingerprint=hashlib.sha256(b"policy").hexdigest(),
        item_count=len(candles),
        issues=(),
    )
    await store.save_candle_page(
        CandlePage(candles, None),
        report,
        normalized_at=start + timedelta(days=4),
        normalizer_version="1.0.0",
    )


def test_intraday_cli_requires_explicit_annualization_periods(
    tmp_path: Path,
) -> None:
    normalized_root = tmp_path / "normalized"
    asyncio.run(_seed_normalized_store(normalized_root))
    with pytest.raises(SystemExit) as error:
        main(
            [
                "backtest",
                "--normalized-root",
                str(normalized_root),
                "--strategy",
                "buy-and-hold",
                "--symbol",
                "ABC",
                "--interval",
                "1m",
            ]
        )
    assert error.value.code == 2
