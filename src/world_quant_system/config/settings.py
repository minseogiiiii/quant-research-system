from enum import StrEnum

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExecutionMode(StrEnum):
    MOCK = "mock"
    REPLAY = "replay"
    SHADOW = "shadow"
    LIVE = "live"


class BrokerProvider(StrEnum):
    NONE = "none"
    TOSS = "toss"


class Settings(BaseSettings):
    execution_mode: ExecutionMode = ExecutionMode.MOCK
    broker_provider: BrokerProvider = BrokerProvider.NONE

    toss_client_id: SecretStr | None = None
    toss_client_secret: SecretStr | None = None
    toss_account_id: SecretStr | None = None

    live_trading_enabled: bool = False

    model_config = SettingsConfigDict(
        env_file=".env.local",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def block_live_trading(self) -> "Settings":
        if (
            self.execution_mode == ExecutionMode.LIVE
            or self.live_trading_enabled
        ):
            raise ValueError(
                "Live trading is disabled during this development stage."
            )

        return self