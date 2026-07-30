from __future__ import annotations

from pathlib import Path

import pytest

from world_quant_system.cli import main

SHA_A = "a" * 64
SHA_B = "b" * 64


def _write_source(path: Path) -> None:
    path.write_text(
        "timestamp,symbol,open,high,low,close,volume,currency\n"
        "2020-01-02T09:00:00+09:00,005930,55000,56000,54500,55500,1000,KRW\n"
        "2020-01-03T09:00:00+09:00,005930,55500,56500,55000,56000,1100,KRW\n"
        "2020-01-06T09:00:00+09:00,005930,56000,57000,55500,56500,1200,KRW\n",
        encoding="utf-8",
    )


def test_dataset_cli_import_validate_freeze_and_list(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source.csv"
    root = tmp_path / "catalog"
    _write_source(source)

    main(
        [
            "dataset",
            "import",
            "--root",
            str(root),
            "--source-file",
            str(source),
            "--provider",
            "localcsv",
            "--exchange",
            "KRX",
            "--symbol",
            "005930",
            "--interval",
            "1d",
            "--timezone",
            "Asia/Seoul",
            "--currency",
            "KRW",
            "--code-commit",
            "a1b2c3d",
            "--point-in-time-context-digest",
            SHA_A,
            "--corporate-action-context-digest",
            SHA_B,
            "--holiday",
            "2020-01-01",
        ]
    )
    imported = capsys.readouterr().out
    assert "State:               imported" in imported
    assert "Network access:       DISABLED" in imported
    dataset_id = next(
        line.split(":", 1)[1].strip()
        for line in imported.splitlines()
        if line.startswith("Dataset ID:")
    )

    main(
        [
            "dataset",
            "validate",
            "--root",
            str(root),
            "--dataset-id",
            dataset_id,
        ]
    )
    assert "Dataset digest:" in capsys.readouterr().out

    main(
        [
            "dataset",
            "freeze",
            "--root",
            str(root),
            "--dataset-id",
            dataset_id,
        ]
    )
    assert "State:               frozen" in capsys.readouterr().out

    main(["dataset", "list", "--root", str(root)])
    listed = capsys.readouterr().out
    assert dataset_id in listed
    assert "frozen" in listed
