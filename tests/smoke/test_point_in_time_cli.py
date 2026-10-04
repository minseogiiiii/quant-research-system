from pathlib import Path

import pytest

from world_quant_system.cli import main

SOURCE_DIGEST = "a" * 64
DATASET_DIGEST = "b" * 64
CODE_COMMIT = "98a9c98f0e321b72dcdab22d7d4fe9f7fac31257"


def test_point_in_time_cli_registers_snapshots_and_validates_access(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "pit"
    main(
        [
            "point-in-time",
            "register-security",
            "--root",
            str(root),
            "--exchange",
            "XKRX",
            "--symbol",
            "005930",
            "--listed-at",
            "1975-06-11",
            "--tradable-from",
            "1975-06-11",
            "--source",
            "test",
            "--source-digest",
            SOURCE_DIGEST,
        ]
    )
    assert "Lifecycle ID:" in capsys.readouterr().out

    main(
        [
            "point-in-time",
            "register-membership",
            "--root",
            str(root),
            "--universe-id",
            "KOSPI",
            "--exchange",
            "XKRX",
            "--symbol",
            "005930",
            "--member-from",
            "2020-01-01",
            "--member-until",
            "2021-01-01",
            "--available-at",
            "2020-01-01",
            "--source",
            "test",
            "--source-digest",
            SOURCE_DIGEST,
        ]
    )
    assert "Membership ID:" in capsys.readouterr().out

    main(
        [
            "point-in-time",
            "register-availability",
            "--root",
            str(root),
            "--data-id",
            "candle-2020-01-02",
            "--data-kind",
            "candle",
            "--exchange",
            "XKRX",
            "--symbol",
            "005930",
            "--effective-at",
            "2020-01-02",
            "--available-at",
            "2020-01-02",
            "--source",
            "test",
            "--source-digest",
            SOURCE_DIGEST,
        ]
    )
    assert "Availability ID:" in capsys.readouterr().out

    main(
        [
            "point-in-time",
            "snapshot",
            "--root",
            str(root),
            "--universe-id",
            "KOSPI",
            "--as-of",
            "2020-06-01",
        ]
    )
    snapshot_output = capsys.readouterr().out
    assert "Members:             1" in snapshot_output
    assert "Snapshot digest:" in snapshot_output

    main(
        [
            "point-in-time",
            "backtest-context",
            "--root",
            str(root),
            "--universe-id",
            "KOSPI",
            "--exchange",
            "XKRX",
            "--symbol",
            "005930",
            "--start",
            "2020-01-01",
            "--end",
            "2021-01-01",
        ]
    )
    context_output = capsys.readouterr().out
    assert "Context digest:" in context_output
    assert "Network access: DISABLED" in context_output

    main(
        [
            "point-in-time",
            "validate-access",
            "--root",
            str(root),
            "--universe-id",
            "KOSPI",
            "--exchange",
            "XKRX",
            "--symbol",
            "005930",
            "--data-id",
            "candle-2020-01-02",
            "--event-at",
            "2020-01-02",
            "--decision-at",
            "2020-01-02",
        ]
    )
    assert "Eligibility:         GRANTED" in capsys.readouterr().out


def test_point_in_time_cli_fails_closed_without_required_metadata(
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            [
                "point-in-time",
                "snapshot",
                "--root",
                str(tmp_path / "pit"),
                "--universe-id",
                "MISSING",
                "--as-of",
                "2020-01-01",
            ]
        )
    assert error.value.code == 2
