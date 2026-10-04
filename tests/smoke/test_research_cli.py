from pathlib import Path

import pytest

from world_quant_system.cli import main

COMMIT = "98a9c98f0e321b72dcdab22d7d4fe9f7fac31257"
DATASET = "a" * 64
RESULT = "b" * 64


def register_arguments(root: Path) -> list[str]:
    return [
        "research",
        "register",
        "--root",
        str(root),
        "--strategy",
        "sma-cross",
        "--dataset-digest",
        DATASET,
        "--code-commit",
        COMMIT,
        "--parameters-json",
        '{"short_window":20,"long_window":100}',
        "--cost-model-json",
        '{"commission_bps":"15"}',
        "--execution-model-json",
        '{"fill":"next_open"}',
        "--train-start",
        "2020-01-01",
        "--train-end",
        "2021-01-01",
        "--validation-start",
        "2021-01-01",
        "--validation-end",
        "2022-01-01",
        "--holdout-start",
        "2022-01-01",
        "--holdout-end",
        "2023-01-01",
    ]


def _experiment_id(output: str) -> str:
    line = next(
        line
        for line in output.splitlines()
        if line.startswith("Experiment ID:")
    )
    return line.split()[-1]


def test_research_cli_registers_inspects_records_and_consumes_holdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "research"
    main(register_arguments(root))
    registered_output = capsys.readouterr().out
    experiment_id = _experiment_id(registered_output)
    assert "Network access: DISABLED" in registered_output
    assert "Holdout state:       UNTOUCHED" in registered_output

    main(
        [
            "research",
            "record-outcome",
            "--root",
            str(root),
            "--experiment-id",
            experiment_id,
            "--status",
            "succeeded",
            "--result-digest",
            RESULT,
        ]
    )
    assert "Status:              succeeded" in capsys.readouterr().out

    main(
        [
            "research",
            "consume-holdout",
            "--root",
            str(root),
            "--experiment-id",
            experiment_id,
            "--result-digest",
            RESULT,
        ]
    )
    assert "Holdout state:       CONSUMED" in capsys.readouterr().out

    main(
        [
            "research",
            "inspect",
            "--root",
            str(root),
            "--experiment-id",
            experiment_id,
        ]
    )
    inspected = capsys.readouterr().out
    assert "Status:              succeeded" in inspected
    assert "Holdout state:       CONSUMED" in inspected

    with pytest.raises(SystemExit) as error:
        main(
            [
                "research",
                "consume-holdout",
                "--root",
                str(root),
                "--experiment-id",
                experiment_id,
                "--result-digest",
                RESULT,
            ]
        )
    assert error.value.code == 2


def test_research_cli_rejects_overlapping_splits(tmp_path: Path) -> None:
    arguments = register_arguments(tmp_path / "research")
    arguments[arguments.index("--validation-start") + 1] = "2020-12-01"

    with pytest.raises(SystemExit) as error:
        main(arguments)

    assert error.value.code == 2
