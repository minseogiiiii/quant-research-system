import pytest

from world_quant_system.credential_isolation.simulation import main


def test_simulation(capsys: pytest.CaptureFixture[str]) -> None:
    main()

    output = capsys.readouterr().out
    assert "deterministic simulation passed" in output
    assert "External network transport: DISABLED" in output
    assert "Broker write count: 0" in output
