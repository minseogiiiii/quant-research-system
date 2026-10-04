from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class CandleInterval(StrEnum):
    MINUTE_1 = "1m"
    DAY_1 = "1d"


def _require_nonblank(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} cannot be empty.")


def _require_currency(value: object) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 3
        or not value.isascii()
        or not value.isalpha()
        or value != value.upper()
    ):
        raise ValueError("Currency must be an uppercase three-letter code.")


def _require_aware_timestamp(value: object, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field_name} must include timezone information.")


def _require_finite_decimal(
    value: object,
    field_name: str,
    *,
    allow_zero: bool,
) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field_name} must be finite.")

    if allow_zero:
        if value < Decimal("0"):
            raise ValueError(f"{field_name} cannot be negative.")
    elif value <= Decimal("0"):
        raise ValueError(f"{field_name} must be greater than zero.")


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    price: Decimal
    timestamp: datetime
    currency: str
    source: str

    def __post_init__(self) -> None:
        _require_nonblank(self.symbol, "Symbol")
        _require_finite_decimal(
            self.price,
            "Price",
            allow_zero=False,
        )
        _require_aware_timestamp(self.timestamp, "Timestamp")
        _require_currency(self.currency)
        _require_nonblank(self.source, "Source")


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str
    interval: CandleInterval
    timestamp: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: int
    currency: str
    source: str

    def __post_init__(self) -> None:
        _require_nonblank(self.symbol, "Symbol")

        if not isinstance(self.interval, CandleInterval):
            raise ValueError("Interval must be a CandleInterval value.")

        _require_aware_timestamp(self.timestamp, "Timestamp")
        _require_currency(self.currency)
        _require_nonblank(self.source, "Source")

        for field_name, value in (
            ("Open price", self.open_price),
            ("High price", self.high_price),
            ("Low price", self.low_price),
            ("Close price", self.close_price),
        ):
            _require_finite_decimal(
                value,
                field_name,
                allow_zero=False,
            )

        if self.high_price < self.low_price:
            raise ValueError("High price cannot be lower than low price.")

        if not self.low_price <= self.open_price <= self.high_price:
            raise ValueError("Open price must be within the candle range.")

        if not self.low_price <= self.close_price <= self.high_price:
            raise ValueError("Close price must be within the candle range.")

        if (
            isinstance(self.volume, bool)
            or not isinstance(self.volume, int)
            or self.volume < 0
        ):
            raise ValueError("Volume must be a nonnegative integer.")


@dataclass(frozen=True, slots=True)
class CandlePage:
    candles: tuple[Candle, ...]
    next_before: datetime | None

    def __post_init__(self) -> None:
        if not isinstance(self.candles, tuple):
            raise ValueError("Candles must be stored as a tuple.")

        if self.next_before is not None:
            _require_aware_timestamp(self.next_before, "Next-before timestamp")

        if not self.candles:
            return

        if not all(isinstance(candle, Candle) for candle in self.candles):
            raise ValueError("Candle page contains an invalid item.")

        first = self.candles[0]
        previous_timestamp = first.timestamp

        for candle in self.candles[1:]:
            if candle.timestamp <= previous_timestamp:
                raise ValueError(
                    "Candles must be strictly ordered from oldest to newest."
                )

            if candle.symbol != first.symbol:
                raise ValueError("Candle page cannot mix symbols.")

            if candle.interval != first.interval:
                raise ValueError("Candle page cannot mix intervals.")

            if candle.currency != first.currency:
                raise ValueError("Candle page cannot mix currencies.")

            if candle.source != first.source:
                raise ValueError("Candle page cannot mix data sources.")

            previous_timestamp = candle.timestamp

        if self.next_before is not None and self.next_before > first.timestamp:
            raise ValueError(
                "Next-before timestamp cannot be later than the oldest candle."
            )


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    quantity: int
    average_price: Decimal

    def __post_init__(self) -> None:
        _require_nonblank(self.symbol, "Symbol")

        if (
            isinstance(self.quantity, bool)
            or not isinstance(self.quantity, int)
            or self.quantity < 0
        ):
            raise ValueError("Quantity must be a nonnegative integer.")

        _require_finite_decimal(
            self.average_price,
            "Average price",
            allow_zero=True,
        )
