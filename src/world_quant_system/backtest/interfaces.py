from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from world_quant_system.backtest.models import (
    BacktestRunResult,
    ExecutionDecision,
    Fill,
    PortfolioSnapshot,
    StrategyDescriptor,
    TargetPositionOrder,
    TargetPositionSignal,
)
from world_quant_system.domain import Candle
from world_quant_system.replay import ReplayEvent


class Strategy(Protocol):
    def reset(self) -> None:
        """Clear per-run mutable state before deterministic replay starts."""
        ...

    @property
    def descriptor(self) -> StrategyDescriptor:
        """Return immutable strategy identity and parameter metadata."""
        ...

    def on_candle(
        self,
        event: ReplayEvent,
        portfolio: PortfolioSnapshot,
    ) -> TargetPositionSignal:
        """Return a long-or-flat target using only the current and past data."""
        ...


class CommissionModel(Protocol):
    def calculate(self, notional: Decimal) -> Decimal:
        """Return deterministic, nonnegative, nondecreasing fill commission."""
        ...


class DividendTaxModel(Protocol):
    @property
    def fingerprint(self) -> str:
        """Return deterministic identity for the dividend tax assumptions."""
        ...

    def calculate(self, gross_dividend: Decimal) -> Decimal:
        """Return deterministic tax withheld from a nonnegative gross dividend."""
        ...


class SlippageModel(Protocol):
    def execution_price(
        self,
        order: TargetPositionOrder,
        reference_price: Decimal,
    ) -> Decimal:
        """Return deterministic execution price for a reference market price."""
        ...


class PositionSizer(Protocol):
    def requested_quantity(
        self,
        order: TargetPositionOrder,
        portfolio: PortfolioSnapshot,
        execution_price: Decimal,
        commission_model: CommissionModel,
    ) -> int:
        """Return desired whole-share quantity before market-volume limits."""
        ...


class ExecutionModel(Protocol):
    def execute(
        self,
        order: TargetPositionOrder,
        candle: Candle,
        portfolio: PortfolioSnapshot,
    ) -> ExecutionDecision:
        """Simulate one next-open fill without broker or network access."""
        ...


class PortfolioLedger(Protocol):
    def apply_fill(self, fill: Fill) -> None:
        """Apply a unique fill to portfolio accounting."""
        ...

    def snapshot(
        self,
        *,
        timestamp: datetime,
        market_price: Decimal,
    ) -> PortfolioSnapshot:
        """Return an immutable mark-to-market snapshot."""
        ...


class BacktestResultWriter(Protocol):
    def write(self, result: BacktestRunResult) -> None:
        """Persist a deterministic backtest result."""
        ...
