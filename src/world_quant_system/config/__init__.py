from enum import StrEnum

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingEnvironment(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class Settings(BaseSettings):
    kis_env: TradingEnvironment = TradingEnvironment.PAPER

    kis_app_key: SecretStr | None = None
    kis_app_secret: SecretStr | None = None
    kis_account_number: SecretStr | None = None
    kis_product_code: str = "01"
    kis_hts_id: SecretStr | None = None

    live_trading_enabled: bool = False

    model_config = SettingsConfigDict(
        env_file=".env.paper",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def block_live_trading(self) -> "Settings":
        if self.kis_env == TradingEnvironment.LIVE or self.live_trading_enabled:
            raise ValueError("Live trading is disabled during this development stage.")

        return self
