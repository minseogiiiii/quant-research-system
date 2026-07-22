from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid5

from world_quant_system.backtest.models import (
    BacktestConfigurationError,
    BacktestInvariantError,
    ClosedTrade,
    Fill,
    OrderSide,
    PortfolioSnapshot,
)

_ZERO = Decimal("0")
_TRADE_NAMESPACE = UUID("c32401d1-8264-50fe-b3f9-a1ff664025d7")


class LongOnlyPortfolioLedger:
    """Decimal-based single-symbol portfolio ledger with duplicate-fill defense."""

    def __init__(self, symbol: str, initial_cash: Decimal) -> None:
        if not isinstance(symbol, str) or not symbol.strip():
            raise BacktestConfigurationError("Portfolio symbol cannot be blank.")
        if (
            not isinstance(initial_cash, Decimal)
            or not initial_cash.is_finite()
            or initial_cash <= _ZERO
        ):
            raise BacktestConfigurationError(
                "Initial cash must be a finite positive Decimal."
            )
        self._symbol = symbol.strip().upper()
        self._initial_cash = initial_cash
        self._cash = initial_cash
        self._quantity = 0
        self._position_cost_basis = _ZERO
        self._realized_pnl = _ZERO
        self._total_commission = _ZERO
        self._total_slippage = _ZERO
        self._turnover_notional = _ZERO
        self._seen_fill_ids: set[str] = set()
        self._seen_order_ids: set[str] = set()
        self._closed_trades: list[ClosedTrade] = []
        self._open_entry_at: datetime | None = None
        self._open_entry_cost = _ZERO
        self._open_exit_proceeds = _ZERO
        self._open_bought_quantity = 0
        self._open_sold_quantity = 0

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def initial_cash(self) -> Decimal:
        return self._initial_cash

    @property
    def closed_trades(self) -> tuple[ClosedTrade, ...]:
        return tuple(self._closed_trades)

    @property
    def turnover_notional(self) -> Decimal:
        return self._turnover_notional

    def apply_fill(self, fill: Fill) -> None:
        if not isinstance(fill, Fill):
            raise BacktestConfigurationError("Portfolio can apply only Fill values.")
        if fill.symbol != self._symbol:
            raise BacktestInvariantError("Fill symbol does not match portfolio symbol.")
        if fill.fill_id in self._seen_fill_ids:
            raise BacktestInvariantError("The same fill cannot be applied twice.")
        if fill.order_id in self._seen_order_ids:
            raise BacktestInvariantError(
                "A target-position order cannot generate multiple fills in v1."
            )

        if fill.side is OrderSide.BUY:
            self._apply_buy(fill)
        else:
            self._apply_sell(fill)

        self._seen_fill_ids.add(fill.fill_id)
        self._seen_order_ids.add(fill.order_id)
        self._total_commission += fill.commission
        self._total_slippage += fill.slippage_cost
        self._turnover_notional += fill.notional
        self._assert_internal_invariants()

    def snapshot(
        self,
        *,
        timestamp: datetime,
        market_price: Decimal,
    ) -> PortfolioSnapshot:
        if (
            not isinstance(timestamp, datetime)
            or timestamp.tzinfo is None
            or timestamp.utcoffset() is None
        ):
            raise BacktestConfigurationError(
                "Portfolio snapshot timestamp must be timezone-aware."
            )
        if (
            not isinstance(market_price, Decimal)
            or not market_price.is_finite()
            or market_price <= _ZERO
        ):
            raise BacktestConfigurationError(
                "Portfolio market price must be a finite positive Decimal."
            )
        market_value = market_price * self._quantity
        total_equity = self._cash + market_value
        if total_equity < _ZERO:
            raise BacktestInvariantError(
                "Long-only portfolio equity cannot be negative."
            )
        average_cost = (
            _ZERO
            if self._quantity == 0
            else self._position_cost_basis / self._quantity
        )
        unrealized_pnl = market_value - self._position_cost_basis
        exposure = _ZERO if total_equity == _ZERO else market_value / total_equity
        return PortfolioSnapshot(
            timestamp=timestamp.astimezone(UTC),
            symbol=self._symbol,
            cash=self._cash,
            quantity=self._quantity,
            position_cost_basis=self._position_cost_basis,
            average_cost=average_cost,
            market_price=market_price,
            market_value=market_value,
            total_equity=total_equity,
            realized_pnl=self._realized_pnl,
            unrealized_pnl=unrealized_pnl,
            total_commission=self._total_commission,
            total_slippage=self._total_slippage,
            exposure=exposure,
        )

    def _apply_buy(self, fill: Fill) -> None:
        total_cost = fill.notional + fill.commission
        if total_cost > self._cash:
            raise BacktestInvariantError("Buy fill exceeds available cash.")
        if self._quantity == 0:
            if self._open_entry_at is not None:
                raise BacktestInvariantError(
                    "Flat portfolio cannot retain an open-trade timestamp."
                )
            self._open_entry_at = fill.filled_at.astimezone(UTC)
            self._open_entry_cost = _ZERO
            self._open_exit_proceeds = _ZERO
            self._open_bought_quantity = 0
            self._open_sold_quantity = 0
        self._cash -= total_cost
        self._quantity += fill.quantity
        self._position_cost_basis += total_cost
        self._open_entry_cost += total_cost
        self._open_bought_quantity += fill.quantity

    def _apply_sell(self, fill: Fill) -> None:
        if fill.quantity > self._quantity:
            raise BacktestInvariantError("Sell fill exceeds the held quantity.")
        entry_at = self._open_entry_at
        if entry_at is None:
            raise BacktestInvariantError("Sell fill requires an open long trade.")
        previous_quantity = self._quantity
        allocated_basis = (
            self._position_cost_basis * Decimal(fill.quantity) / previous_quantity
        )
        net_proceeds = fill.notional - fill.commission
        self._cash += net_proceeds
        self._quantity -= fill.quantity
        self._position_cost_basis -= allocated_basis
        self._realized_pnl += net_proceeds - allocated_basis
        self._open_exit_proceeds += net_proceeds
        self._open_sold_quantity += fill.quantity

        if self._quantity == 0:
            self._position_cost_basis = _ZERO
            trade_id = str(
                uuid5(
                    _TRADE_NAMESPACE,
                    "|".join(
                        (
                            self._symbol,
                            entry_at.isoformat(),
                            fill.filled_at.astimezone(UTC).isoformat(),
                            str(len(self._closed_trades)),
                        )
                    ),
                )
            )
            net_pnl = self._open_exit_proceeds - self._open_entry_cost
            self._closed_trades.append(
                ClosedTrade(
                    trade_id=trade_id,
                    symbol=self._symbol,
                    entry_at=entry_at,
                    exit_at=fill.filled_at.astimezone(UTC),
                    bought_quantity=self._open_bought_quantity,
                    sold_quantity=self._open_sold_quantity,
                    entry_cost=self._open_entry_cost,
                    exit_proceeds=self._open_exit_proceeds,
                    net_pnl=net_pnl,
                    return_fraction=net_pnl / self._open_entry_cost,
                )
            )
            self._open_entry_at = None
            self._open_entry_cost = _ZERO
            self._open_exit_proceeds = _ZERO
            self._open_bought_quantity = 0
            self._open_sold_quantity = 0

    def _assert_internal_invariants(self) -> None:
        if self._cash < _ZERO:
            raise BacktestInvariantError("Long-only portfolio cash cannot be negative.")
        if self._quantity < 0:
            raise BacktestInvariantError(
                "Long-only portfolio quantity cannot be negative."
            )
        if self._position_cost_basis < _ZERO:
            raise BacktestInvariantError("Position cost basis cannot be negative.")
        if self._quantity == 0 and self._position_cost_basis != _ZERO:
            raise BacktestInvariantError(
                "Flat portfolio cannot retain position cost basis."
            )
