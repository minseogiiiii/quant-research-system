from pathlib import Path

import pytest
from pydantic import ValidationError

from world_quant_system.config.settings import (
    BrokerProvider,
    ExecutionMode,
    Settings,
)


def test_settings_default_to_safe_mock_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.execution_mode == ExecutionMode.MOCK
    assert settings.broker_provider == BrokerProvider.NONE
    assert settings.live_trading_enabled is False


def test_settings_reject_live_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError):
        Settings(
            execution_mode=ExecutionMode.LIVE,
        )


def test_settings_reject_live_trading_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError):
        Settings(
            live_trading_enabled=True,
        )


def test_shadow_mode_allows_toss_read_only_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    settings = Settings(
        execution_mode=ExecutionMode.SHADOW,
        broker_provider=BrokerProvider.TOSS,
    )

    assert settings.execution_mode == ExecutionMode.SHADOW
    assert settings.broker_provider == BrokerProvider.TOSS
    assert settings.live_trading_enabled is False