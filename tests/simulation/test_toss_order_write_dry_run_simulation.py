import pytest

from world_quant_system.order_write_certification.simulation import main


def test_simulation(capsys: pytest.CaptureFixture[str]) -> None:
    main()
    output = capsys.readouterr().out
    assert "deterministic simulation passed" in output
    assert "Broker write count: 0" in output
    assert "Order submission: NOT SUBMITTED" in output
