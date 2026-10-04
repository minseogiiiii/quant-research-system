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
from world_quant_system.research import (
    CorporateActionRecord,
    CorporateActionType,
    SQLiteCorporateActionStore,
)

SOURCE_DIGEST = "a" * 64


def _action_id(output: str) -> str:
    line = next(line for line in output.splitlines() if line.startswith("Action ID:"))
    return line.split()[-1]


def test_corporate_action_cli_registers_inspects_lists_and_builds_context(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "corporate-actions"
    main(
        [
            "corporate-actions",
            "register",
            "--root",
            str(root),
            "--exchange",
            "XNAS",
            "--symbol",
            "ABC",
            "--type",
            "split",
            "--effective-at",
            "2026-01-03",
            "--available-at",
            "2026-01-01",
            "--source",
            "test",
            "--source-digest",
            SOURCE_DIGEST,
            "--ratio-numerator",
            "2",
            "--ratio-denominator",
            "1",
        ]
    )
    registered = capsys.readouterr().out
    action_id = _action_id(registered)
    assert "Network access: DISABLED" in registered
    assert "Order submission: DISABLED" in registered

    main(
        [
            "corporate-actions",
            "inspect",
            "--root",
            str(root),
            "--action-id",
            action_id,
        ]
    )
    assert "Action type:         split" in capsys.readouterr().out

    main(
        [
            "corporate-actions",
            "list",
            "--root",
            str(root),
            "--exchange",
            "XNAS",
            "--symbol",
            "ABC",
            "--start",
            "2026-01-01",
            "--end",
            "2026-02-01",
        ]
    )
    listed = capsys.readouterr().out
    assert "Actions:              1" in listed
    assert action_id in listed

    main(
        [
            "corporate-actions",
            "backtest-context",
            "--root",
            str(root),
            "--exchange",
            "XNAS",
            "--symbol",
            "ABC",
            "--start",
            "2026-01-01",
            "--end",
            "2026-02-01",
        ]
    )
    context = capsys.readouterr().out
    assert "Actions:             1" in context
    assert "Dataset digest:" in context
    assert "Context digest:" in context


def test_corporate_action_cli_rejects_delisting_without_settlement(
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            [
                "corporate-actions",
                "register",
                "--root",
                str(tmp_path / "corporate-actions"),
                "--exchange",
                "XNAS",
                "--symbol",
                "ABC",
                "--type",
                "delisting",
                "--effective-at",
                "2026-01-03",
                "--available-at",
                "2026-01-01",
                "--source",
                "test",
                "--source-digest",
                SOURCE_DIGEST,
            ]
        )
    assert error.value.code == 2


def test_backtest_cli_applies_split_and_writes_action_audit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    normalized_root = tmp_path / "normalized"
    action_root = tmp_path / "corporate-actions"
    asyncio.run(_seed_split_backtest(normalized_root, action_root))
    output_path = tmp_path / "summary.json"

    main(
        [
            "backtest",
            "--normalized-root",
            str(normalized_root),
            "--corporate-action-root",
            str(action_root),
            "--exchange",
            "XNAS",
            "--strategy",
            "buy-and-hold",
            "--symbol",
            "ABC",
            "--start",
            "2026-01-01",
            "--end",
            "2026-01-04",
            "--initial-cash",
            "1000",
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
    assert "Corporate actions:   1" in output
    assert "Corporate context:" in output
    assert "NOT_APPLIED" not in output
    summary = json.loads(output_path.read_text(encoding="utf-8"))
    assert summary["corporate_action_count"] == 1
    assert summary["corporate_action_type_counts"]["split"] == 1
    assert summary["metrics"]["final_equity"] == "1000"


async def _seed_split_backtest(
    normalized_root: Path,
    action_root: Path,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    prices = (Decimal("10"), Decimal("10"), Decimal("5"), Decimal("5"))
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
        for index, price in enumerate(prices)
    )
    report = DataQualityReport(
        report_id=str(uuid4()),
        assessment_key=hashlib.sha256(b"assessment").hexdigest(),
        record_id=str(uuid4()),
        raw_content_sha256=hashlib.sha256(b"raw").hexdigest(),
        dataset_kind=QualityDatasetKind.CANDLES,
        status=QualityStatus.PASS,
        checked_at=start + timedelta(days=5),
        validator_version="1.0.0",
        policy_fingerprint=hashlib.sha256(b"policy").hexdigest(),
        item_count=len(candles),
        issues=(),
    )
    normalized = SQLiteNormalizedMarketDataStore(normalized_root)
    await normalized.save_candle_page(
        CandlePage(candles, None),
        report,
        normalized_at=start + timedelta(days=5),
        normalizer_version="1.0.0",
    )
    actions = SQLiteCorporateActionStore(action_root)
    await actions.save_action(
        CorporateActionRecord(
            exchange="XNAS",
            symbol="ABC",
            action_type=CorporateActionType.SPLIT,
            effective_at=start + timedelta(days=2),
            available_at=start,
            source="test",
            source_digest=SOURCE_DIGEST,
            ratio_numerator=2,
            ratio_denominator=1,
        )
    )
