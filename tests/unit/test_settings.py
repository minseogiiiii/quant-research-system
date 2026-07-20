import pytest
from pydantic import ValidationError

from world_quant_system.config.settings import (
    Settings,
    TradingEnvironment,
)


def test_settings_default_to_paper_trading() -> None:
    settings = Settings(_env_file=None)

    assert settings.kis_env == TradingEnvironment.PAPER
    assert settings.live_trading_enabled is False


def test_settings_reject_live_environment() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            kis_env=TradingEnvironment.LIVE,
        )


def test_settings_reject_live_trading_flag() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            live_trading_enabled=True,
        )