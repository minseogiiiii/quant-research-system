from __future__ import annotations

import pytest

from world_quant_system.disabled_write_transport.simulation import main


def test_simulation(capsys: pytest.CaptureFixture[str]) -> None:
    main()
    output = capsys.readouterr().out
    assert "Disabled write transport deterministic simulation passed." in output
    assert "External network calls: 0" in output
    assert "Broker write count: 0" in output
    assert "Final state: TRANSPORT_BLOCKED" in output
