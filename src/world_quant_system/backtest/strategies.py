from __future__ import annotations

from collections import deque
from decimal import Decimal
from uuid import UUID, uuid5

from world_quant_system.backtest.models import (
    BacktestConfigurationError,
    BacktestInvariantError,
    PortfolioSnapshot,
    StrategyDescriptor,
    TargetPositionSignal,
)
from world_quant_system.replay import ReplayEvent

_SIGNAL_NAMESPACE = UUID("f2850268-f0c5-5e79-b1a7-c0bc17437518")
_ZERO = Decimal("0")
_ONE = Decimal("1")


class BuyAndHoldStrategy:
    """Long-only baseline that requests entry after the first observed close."""

    def __init__(self) -> None:
        self._descriptor = StrategyDescriptor(
            name="buy-and-hold",
            version="1.0.0",
            parameters=(),
        )

    def reset(self) -> None:
        """Buy-and-hold is stateless across runs."""
        return None

    @property
    def descriptor(self) -> StrategyDescriptor:
        return self._descriptor

    def on_candle(
        self,
        event: ReplayEvent,
        portfolio: PortfolioSnapshot,
    ) -> TargetPositionSignal:
        _validate_context(event, portfolio)
        return _signal(
            descriptor=self._descriptor,
            event=event,
            target_fraction=_ONE,
            reason="Remain fully invested after the first observed close.",
        )


class SmaCrossoverStrategy:
    """O(1) rolling SMA crossover that uses closes observed so far only."""

    def __init__(self, *, short_window: int, long_window: int) -> None:
        if isinstance(short_window, bool) or not isinstance(short_window, int):
            raise BacktestConfigurationError(
                "Short SMA window must be an integer."
            )
        if isinstance(long_window, bool) or not isinstance(long_window, int):
            raise BacktestConfigurationError("Long SMA window must be an integer.")
        if short_window <= 0 or long_window <= 0:
            raise BacktestConfigurationError("SMA windows must be positive.")
        if short_window >= long_window:
            raise BacktestConfigurationError(
                "Short SMA window must be smaller than long SMA window."
            )
        self._short_window = short_window
        self._long_window = long_window
        self._closes: deque[Decimal] = deque()
        self._short_sum = Decimal("0")
        self._long_sum = Decimal("0")
        self._descriptor = StrategyDescriptor(
            name="sma-crossover",
            version="1.0.0",
            parameters=(
                ("long_window", str(long_window)),
                ("short_window", str(short_window)),
            ),
        )

    def reset(self) -> None:
        self._closes.clear()
        self._short_sum = Decimal("0")
        self._long_sum = Decimal("0")

    @property
    def descriptor(self) -> StrategyDescriptor:
        return self._descriptor

    def on_candle(
        self,
        event: ReplayEvent,
        portfolio: PortfolioSnapshot,
    ) -> TargetPositionSignal:
        _validate_context(event, portfolio)
        close = event.record.candle.close_price
        self._closes.append(close)
        self._long_sum += close
        self._short_sum += close

        if len(self._closes) > self._short_window:
            self._short_sum -= self._closes[-self._short_window - 1]
        if len(self._closes) > self._long_window:
            removed = self._closes.popleft()
            self._long_sum -= removed

        if len(self._closes) < self._long_window:
            return _signal(
                descriptor=self._descriptor,
                event=event,
                target_fraction=_ZERO,
                reason=(
                    f"Warm-up {len(self._closes)}/{self._long_window}; remain flat."
                ),
            )

        short_average = self._short_sum / self._short_window
        long_average = self._long_sum / self._long_window
        target = _ONE if short_average > long_average else _ZERO
        relation = ">" if target == _ONE else "<="
        return _signal(
            descriptor=self._descriptor,
            event=event,
            target_fraction=target,
            reason=(
                f"short_sma={short_average} {relation} long_sma={long_average}"
            ),
        )


def _signal(
    *,
    descriptor: StrategyDescriptor,
    event: ReplayEvent,
    target_fraction: Decimal,
    reason: str,
) -> TargetPositionSignal:
    signal_id = str(
        uuid5(
            _SIGNAL_NAMESPACE,
            "|".join(
                (
                    descriptor.fingerprint,
                    event.record.item_id,
                    format(target_fraction, "f"),
                )
            ),
        )
    )
    return TargetPositionSignal(
        signal_id=signal_id,
        symbol=event.record.candle.symbol,
        generated_at=event.event_time,
        target_fraction=target_fraction,
        reason=reason,
    )


def _validate_context(event: ReplayEvent, portfolio: PortfolioSnapshot) -> None:
    if not isinstance(event, ReplayEvent):
        raise BacktestConfigurationError("Strategy requires a ReplayEvent.")
    if not isinstance(portfolio, PortfolioSnapshot):
        raise BacktestConfigurationError(
            "Strategy requires a PortfolioSnapshot."
        )
    if event.record.candle.symbol != portfolio.symbol:
        raise BacktestInvariantError(
            "Strategy event and portfolio symbols must match."
        )
    if event.event_time != portfolio.timestamp:
        raise BacktestInvariantError(
            "Strategy can only observe a portfolio marked at the current candle."
        )
