import pytest

from world_quant_system.broker_certification.simulation import main


def test_simulation(capsys: pytest.CaptureFixture[str]) -> None:
    main()
    output = capsys.readouterr().out
    assert "certification simulation passed" in output
    assert "Network transport: DISABLED" in output
    assert "Write operations: DISABLED" in output
