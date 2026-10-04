import pytest

from world_quant_system.toss_live_read.simulation import main


def test_simulation(capsys: pytest.CaptureFixture[str]) -> None:
    main()
    output = capsys.readouterr().out
    assert "deterministic simulation passed" in output
    assert "Broker writes: DISABLED" in output
