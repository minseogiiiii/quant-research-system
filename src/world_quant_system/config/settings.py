from enum import StrEnum

from pydantic import model_validator
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
    live_trading_enabled: bool = False

    model_config = SettingsConfigDict(
        env_file=".env.local",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_safe_configuration(self) -> "Settings":
        if self.execution_mode == ExecutionMode.LIVE:
            raise ValueError(
                "Live execution mode is disabled during this development stage."
            )

        if self.live_trading_enabled:
            raise ValueError("Live trading is disabled during this development stage.")

        if (
            self.execution_mode in {ExecutionMode.MOCK, ExecutionMode.REPLAY}
            and self.broker_provider != BrokerProvider.NONE
        ):
            raise ValueError(
                "Mock and replay modes must not connect to a broker provider."
            )

        if (
            self.execution_mode == ExecutionMode.SHADOW
            and self.broker_provider != BrokerProvider.TOSS
        ):
            raise ValueError("Shadow mode requires the read-only Toss broker provider.")

        return self
