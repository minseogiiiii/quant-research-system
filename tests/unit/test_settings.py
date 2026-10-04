from pathlib import Path

import pytest
from pydantic import ValidationError

from world_quant_system.config.settings import (
    BrokerProvider,
    ExecutionMode,
    Settings,
)


def load_settings_without_local_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    execution_mode: ExecutionMode = ExecutionMode.MOCK,
    broker_provider: BrokerProvider = BrokerProvider.NONE,
    live_trading_enabled: bool = False,
) -> Settings:
    monkeypatch.chdir(tmp_path)
    return Settings(
        execution_mode=execution_mode,
        broker_provider=broker_provider,
        live_trading_enabled=live_trading_enabled,
    )


def test_settings_default_to_safe_mock_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = load_settings_without_local_env(monkeypatch, tmp_path)

    assert settings.execution_mode == ExecutionMode.MOCK
    assert settings.broker_provider == BrokerProvider.NONE
    assert settings.live_trading_enabled is False


def test_settings_reject_live_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError):
        load_settings_without_local_env(
            monkeypatch,
            tmp_path,
            execution_mode=ExecutionMode.LIVE,
        )


def test_settings_reject_live_trading_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError):
        load_settings_without_local_env(
            monkeypatch,
            tmp_path,
            live_trading_enabled=True,
        )


@pytest.mark.parametrize("mode", [ExecutionMode.MOCK, ExecutionMode.REPLAY])
def test_offline_modes_reject_broker_connections(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: ExecutionMode,
) -> None:
    with pytest.raises(ValidationError):
        load_settings_without_local_env(
            monkeypatch,
            tmp_path,
            execution_mode=mode,
            broker_provider=BrokerProvider.TOSS,
        )


def test_shadow_mode_requires_toss_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError):
        load_settings_without_local_env(
            monkeypatch,
            tmp_path,
            execution_mode=ExecutionMode.SHADOW,
            broker_provider=BrokerProvider.NONE,
        )


def test_shadow_mode_allows_read_only_toss_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = load_settings_without_local_env(
        monkeypatch,
        tmp_path,
        execution_mode=ExecutionMode.SHADOW,
        broker_provider=BrokerProvider.TOSS,
    )

    assert settings.execution_mode == ExecutionMode.SHADOW
    assert settings.broker_provider == BrokerProvider.TOSS
    assert settings.live_trading_enabled is False
