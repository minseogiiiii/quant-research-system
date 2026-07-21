from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    price: Decimal
    timestamp: datetime
    source: str

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("Symbol cannot be empty.")

        if self.price <= Decimal("0"):
            raise ValueError("Price must be greater than zero.")

        if self.timestamp.tzinfo is None:
            raise ValueError("Timestamp must include timezone information.")


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    quantity: int
    average_price: Decimal

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("Symbol cannot be empty.")

        if self.quantity < 0:
            raise ValueError("Quantity cannot be negative.")

        if self.average_price < Decimal("0"):
            raise ValueError("Average price cannot be negative.")
