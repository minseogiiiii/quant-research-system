from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from world_quant_system.data.normalized_models import canonical_json_bytes, format_utc
from world_quant_system.replay import ReplayConfig, ReplayRunResult

_ZERO = Decimal("0")
_ONE = Decimal("1")
_BPS_DENOMINATOR = Decimal("10000")


class BacktestError(Exception):
    """Base exception for backtest failures."""


class BacktestConfigurationError(BacktestError):
    """Raised when backtest configuration is invalid."""


class BacktestInvariantError(BacktestError):
    """Raised when deterministic backtest invariants are violated."""


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(StrEnum):
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class OrderRejectReason(StrEnum):
    INSUFFICIENT_CASH = "insufficient_cash"
    NO_POSITION = "no_position"
    NO_EXECUTABLE_VOLUME = "no_executable_volume"
    ZERO_QUANTITY = "zero_quantity"
    NO_NEXT_CANDLE = "no_next_candle"


@dataclass(frozen=True, slots=True)
class StrategyDescriptor:
    name: str
    version: str
    parameters: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _require_nonblank(self.name, "Strategy name")
        _require_nonblank(self.version, "Strategy version")
        if not isinstance(self.parameters, tuple):
            raise BacktestConfigurationError(
                "Strategy parameters must be stored as a tuple."
            )
        previous_name: str | None = None
        for parameter_name, parameter_value in self.parameters:
            _require_nonblank(parameter_name, "Strategy parameter name")
            _require_nonblank(parameter_value, "Strategy parameter value")
            if previous_name is not None and parameter_name <= previous_name:
                raise BacktestConfigurationError(
                    "Strategy parameters must be uniquely sorted by name."
                )
            previous_name = parameter_name

    @property
    def fingerprint(self) -> str:
        document = {
            "name": self.name,
            "version": self.version,
            "parameters": [
                {"name": name, "value": value} for name, value in self.parameters
            ],
        }
        return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


@dataclass(frozen=True, slots=True)
class TargetPositionSignal:
    signal_id: str
    symbol: str
    generated_at: datetime
    target_fraction: Decimal
    reason: str

    def __post_init__(self) -> None:
        _validate_uuid(self.signal_id, "Signal ID")
        _require_nonblank(self.symbol, "Signal symbol")
        _require_aware(self.generated_at, "Signal timestamp")
        _require_finite_decimal(self.target_fraction, "Target fraction")
        if self.target_fraction not in (_ZERO, _ONE):
            raise BacktestConfigurationError(
                "Backtest v1 supports only long-or-flat target fractions 0 and 1."
            )
        _require_nonblank(self.reason, "Signal reason")


@dataclass(frozen=True, slots=True)
class TargetPositionOrder:
    order_id: str
    signal_id: str
    symbol: str
    side: OrderSide
    requested_at: datetime
    target_fraction: Decimal
    reference_price: Decimal

    def __post_init__(self) -> None:
        _validate_uuid(self.order_id, "Order ID")
        _validate_uuid(self.signal_id, "Signal ID")
        _require_nonblank(self.symbol, "Order symbol")
        if not isinstance(self.side, OrderSide):
            raise BacktestConfigurationError("Order side must be an OrderSide value.")
        _require_aware(self.requested_at, "Order request timestamp")
        _require_finite_positive_decimal(self.reference_price, "Reference price")
        if self.target_fraction not in (_ZERO, _ONE):
            raise BacktestConfigurationError(
                "Backtest v1 supports only long-or-flat target fractions 0 and 1."
            )
        expected_side = (
            OrderSide.BUY if self.target_fraction == _ONE else OrderSide.SELL
        )
        if self.side is not expected_side:
            raise BacktestConfigurationError(
                "Order side does not match the requested target fraction."
            )


@dataclass(frozen=True, slots=True)
class Fill:
    fill_id: str
    order_id: str
    symbol: str
    side: OrderSide
    quantity: int
    reference_price: Decimal
    execution_price: Decimal
    notional: Decimal
    commission: Decimal
    slippage_cost: Decimal
    filled_at: datetime

    def __post_init__(self) -> None:
        _validate_uuid(self.fill_id, "Fill ID")
        _validate_uuid(self.order_id, "Order ID")
        _require_nonblank(self.symbol, "Fill symbol")
        if not isinstance(self.side, OrderSide):
            raise BacktestConfigurationError("Fill side must be an OrderSide value.")
        _require_positive_int(self.quantity, "Fill quantity")
        _require_finite_positive_decimal(self.reference_price, "Fill reference price")
        _require_finite_positive_decimal(self.execution_price, "Execution price")
        _require_finite_positive_decimal(self.notional, "Fill notional")
        _require_finite_nonnegative_decimal(self.commission, "Commission")
        _require_finite_nonnegative_decimal(self.slippage_cost, "Slippage cost")
        _require_aware(self.filled_at, "Fill timestamp")
        if self.notional != self.execution_price * self.quantity:
            raise BacktestInvariantError(
                "Fill notional must equal execution price times quantity."
            )
        expected_slippage = abs(self.execution_price - self.reference_price) * Decimal(
            self.quantity
        )
        if self.slippage_cost != expected_slippage:
            raise BacktestInvariantError(
                "Fill slippage cost does not match reference and execution prices."
            )


@dataclass(frozen=True, slots=True)
class ExecutionDecision:
    fill: Fill | None
    reject_reason: OrderRejectReason | None
    requested_quantity: int
    target_quantity: int

    def __post_init__(self) -> None:
        _require_nonnegative_int(self.requested_quantity, "Requested quantity")
        _require_nonnegative_int(self.target_quantity, "Target quantity")
        if self.fill is None and self.reject_reason is None:
            raise BacktestInvariantError(
                "Rejected execution decisions require a reject reason."
            )
        if self.fill is not None and self.reject_reason is not None:
            raise BacktestInvariantError(
                "Successful execution decisions cannot contain a reject reason."
            )


@dataclass(frozen=True, slots=True)
class OrderRecord:
    order: TargetPositionOrder
    status: OrderStatus
    requested_quantity: int | None
    target_quantity: int | None
    quantity: int
    reject_reason: OrderRejectReason | None
    fill_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.order, TargetPositionOrder):
            raise BacktestConfigurationError(
                "Order record must contain a TargetPositionOrder."
            )
        if not isinstance(self.status, OrderStatus):
            raise BacktestConfigurationError(
                "Order record status must be an OrderStatus value."
            )
        _require_optional_nonnegative_int(
            self.requested_quantity,
            "Requested quantity",
        )
        _require_optional_nonnegative_int(
            self.target_quantity,
            "Target quantity",
        )
        _require_nonnegative_int(self.quantity, "Order record quantity")
        if self.status is OrderStatus.EXPIRED:
            if self.requested_quantity is not None or self.target_quantity is not None:
                raise BacktestInvariantError(
                    "Expired orders cannot contain execution quantities."
                )
            if self.quantity != 0 or self.fill_id is not None:
                raise BacktestInvariantError(
                    "Expired orders cannot contain a fill."
                )
            if self.reject_reason is not OrderRejectReason.NO_NEXT_CANDLE:
                raise BacktestInvariantError(
                    "Expired orders require the no-next-candle reason."
                )
            return

        if self.requested_quantity is None or self.target_quantity is None:
            raise BacktestInvariantError(
                "Executed or rejected orders require execution quantities."
            )
        if self.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            if (
                self.quantity <= 0
                or self.fill_id is None
                or self.reject_reason is not None
            ):
                raise BacktestInvariantError(
                    "Filled order records require quantity and fill ID only."
                )
            _validate_uuid(self.fill_id, "Fill ID")
            if self.status is OrderStatus.FILLED:
                if self.quantity != self.requested_quantity:
                    raise BacktestInvariantError(
                        "Filled orders must satisfy the requested quantity."
                    )
            elif not self.quantity < self.requested_quantity:
                raise BacktestInvariantError(
                    "Partially filled orders must fill less than requested."
                )
        else:
            if self.quantity != 0 or self.fill_id is not None:
                raise BacktestInvariantError(
                    "Rejected orders cannot contain a fill."
                )
            if self.reject_reason is None:
                raise BacktestInvariantError(
                    "Rejected orders require a reason."
                )


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    timestamp: datetime
    symbol: str
    cash: Decimal
    quantity: int
    position_cost_basis: Decimal
    average_cost: Decimal
    market_price: Decimal
    market_value: Decimal
    total_equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_commission: Decimal
    total_slippage: Decimal
    exposure: Decimal

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "Portfolio snapshot timestamp")
        _require_nonblank(self.symbol, "Portfolio symbol")
        _require_finite_nonnegative_decimal(self.cash, "Portfolio cash")
        _require_nonnegative_int(self.quantity, "Portfolio quantity")
        for field_name, field_value in (
            ("Position cost basis", self.position_cost_basis),
            ("Average cost", self.average_cost),
            ("Market value", self.market_value),
            ("Total equity", self.total_equity),
            ("Total commission", self.total_commission),
            ("Total slippage", self.total_slippage),
            ("Exposure", self.exposure),
        ):
            _require_finite_nonnegative_decimal(field_value, field_name)
        _require_finite_positive_decimal(self.market_price, "Market price")
        _require_finite_decimal(self.realized_pnl, "Realized PnL")
        _require_finite_decimal(self.unrealized_pnl, "Unrealized PnL")
        if self.market_value != self.market_price * self.quantity:
            raise BacktestInvariantError(
                "Portfolio market value must equal market price times quantity."
            )
        if self.total_equity != self.cash + self.market_value:
            raise BacktestInvariantError(
                "Portfolio equity must equal cash plus market value."
            )
        if (
            self.quantity == 0
            and (self.position_cost_basis != _ZERO or self.average_cost != _ZERO)
        ):
            raise BacktestInvariantError(
                "Flat portfolios cannot retain position cost basis."
            )
        if (
            self.quantity > 0
            and self.average_cost != self.position_cost_basis / self.quantity
        ):
            raise BacktestInvariantError(
                "Average cost must equal position cost basis divided by quantity."
            )
        expected_unrealized = self.market_value - self.position_cost_basis
        if self.unrealized_pnl != expected_unrealized:
            raise BacktestInvariantError(
                "Unrealized PnL must equal market value minus position cost basis."
            )
        expected_exposure = (
            _ZERO
            if self.total_equity == _ZERO
            else self.market_value / self.total_equity
        )
        if self.exposure != expected_exposure:
            raise BacktestInvariantError(
                "Portfolio exposure does not match market value and equity."
            )
        if self.exposure > _ONE:
            raise BacktestInvariantError(
                "Long-only unlevered portfolio exposure cannot exceed 1."
            )


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    trade_id: str
    symbol: str
    entry_at: datetime
    exit_at: datetime
    bought_quantity: int
    sold_quantity: int
    entry_cost: Decimal
    exit_proceeds: Decimal
    net_pnl: Decimal
    return_fraction: Decimal

    def __post_init__(self) -> None:
        _validate_uuid(self.trade_id, "Trade ID")
        _require_nonblank(self.symbol, "Trade symbol")
        _require_aware(self.entry_at, "Trade entry timestamp")
        _require_aware(self.exit_at, "Trade exit timestamp")
        if self.exit_at <= self.entry_at:
            raise BacktestInvariantError(
                "Trade exit timestamp must follow entry timestamp."
            )
        _require_positive_int(self.bought_quantity, "Bought quantity")
        _require_positive_int(self.sold_quantity, "Sold quantity")
        if self.bought_quantity != self.sold_quantity:
            raise BacktestInvariantError(
                "Closed long-only trades must buy and sell equal quantities."
            )
        _require_finite_positive_decimal(self.entry_cost, "Trade entry cost")
        _require_finite_nonnegative_decimal(self.exit_proceeds, "Trade exit proceeds")
        _require_finite_decimal(self.net_pnl, "Trade net PnL")
        _require_finite_decimal(self.return_fraction, "Trade return")
        if self.net_pnl != self.exit_proceeds - self.entry_cost:
            raise BacktestInvariantError(
                "Trade net PnL must equal exit proceeds minus entry cost."
            )
        if self.return_fraction != self.net_pnl / self.entry_cost:
            raise BacktestInvariantError(
                "Trade return must equal net PnL divided by entry cost."
            )


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    initial_equity: Decimal
    final_equity: Decimal
    total_return: Decimal
    cagr: float | None
    maximum_drawdown: Decimal
    annualized_volatility: float | None
    sharpe_ratio: float | None
    sortino_ratio: float | None
    calmar_ratio: float | None
    win_rate: Decimal | None
    profit_factor: Decimal | None
    average_win: Decimal | None
    average_loss: Decimal | None
    trade_count: int
    turnover: Decimal
    average_exposure: Decimal
    commission_cost: Decimal
    slippage_cost: Decimal
    benchmark_return: Decimal | None
    excess_return: Decimal | None

    def __post_init__(self) -> None:
        _require_finite_positive_decimal(self.initial_equity, "Initial equity")
        _require_finite_nonnegative_decimal(self.final_equity, "Final equity")
        _require_finite_decimal(self.total_return, "Total return")
        _require_finite_nonpositive_decimal(
            self.maximum_drawdown, "Maximum drawdown"
        )
        for ratio_name, ratio_value in (
            ("CAGR", self.cagr),
            ("Annualized volatility", self.annualized_volatility),
            ("Sharpe ratio", self.sharpe_ratio),
            ("Sortino ratio", self.sortino_ratio),
            ("Calmar ratio", self.calmar_ratio),
        ):
            _require_optional_finite_float(ratio_value, ratio_name)
        for field_name, field_value in (
            ("Win rate", self.win_rate),
            ("Profit factor", self.profit_factor),
            ("Average win", self.average_win),
            ("Average loss", self.average_loss),
            ("Benchmark return", self.benchmark_return),
            ("Excess return", self.excess_return),
        ):
            _require_optional_finite_decimal(field_value, field_name)
        _require_nonnegative_int(self.trade_count, "Trade count")
        for field_name, field_value in (
            ("Turnover", self.turnover),
            ("Average exposure", self.average_exposure),
            ("Commission cost", self.commission_cost),
            ("Slippage cost", self.slippage_cost),
        ):
            _require_finite_nonnegative_decimal(field_value, field_name)
        if self.final_equity != self.initial_equity * (_ONE + self.total_return):
            raise BacktestInvariantError(
                "Final equity must match initial equity and total return."
            )
        if self.total_return < -_ONE:
            raise BacktestInvariantError("Total return cannot be below -100%.")
        if self.maximum_drawdown < -_ONE:
            raise BacktestInvariantError(
                "Maximum drawdown cannot be below -100%."
            )
        if self.annualized_volatility is not None and self.annualized_volatility < 0:
            raise BacktestInvariantError(
                "Annualized volatility cannot be negative."
            )
        if self.win_rate is not None and not _ZERO <= self.win_rate <= _ONE:
            raise BacktestInvariantError("Win rate must be between 0 and 1.")
        if self.trade_count == 0 and self.win_rate is not None:
            raise BacktestInvariantError(
                "Zero closed trades require an undefined win rate."
            )
        if self.trade_count > 0 and self.win_rate is None:
            raise BacktestInvariantError(
                "Closed trades require a defined win rate."
            )
        if self.profit_factor is not None and self.profit_factor < _ZERO:
            raise BacktestInvariantError("Profit factor cannot be negative.")
        if self.average_win is not None and self.average_win <= _ZERO:
            raise BacktestInvariantError("Average win must be positive when defined.")
        if self.average_loss is not None and self.average_loss >= _ZERO:
            raise BacktestInvariantError("Average loss must be negative when defined.")
        if self.average_exposure > _ONE:
            raise BacktestInvariantError(
                "Long-only unlevered average exposure cannot exceed 1."
            )


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    replay: ReplayConfig
    initial_cash: Decimal
    commission_bps: Decimal = Decimal("15")
    slippage_bps: Decimal = Decimal("10")
    max_volume_participation: Decimal = Decimal("0.10")
    annualization_periods: int = 252

    def __post_init__(self) -> None:
        if not isinstance(self.replay, ReplayConfig):
            raise BacktestConfigurationError(
                "Backtest replay configuration must be a ReplayConfig."
            )
        if len(self.replay.symbols) != 1:
            raise BacktestConfigurationError(
                "Backtest v1 supports exactly one symbol per run."
            )
        _require_finite_positive_decimal(self.initial_cash, "Initial cash")
        _require_finite_nonnegative_decimal(
            self.commission_bps, "Commission bps"
        )
        if self.commission_bps > _BPS_DENOMINATOR:
            raise BacktestConfigurationError(
                "Commission bps cannot exceed 10000."
            )
        _require_finite_nonnegative_decimal(self.slippage_bps, "Slippage bps")
        if self.slippage_bps >= _BPS_DENOMINATOR:
            raise BacktestConfigurationError(
                "Slippage bps must be less than 10000."
            )
        _require_finite_positive_decimal(
            self.max_volume_participation, "Maximum volume participation"
        )
        if self.max_volume_participation > _ONE:
            raise BacktestConfigurationError(
                "Maximum volume participation cannot exceed 1."
            )
        _require_positive_int(self.annualization_periods, "Annualization periods")

    @property
    def symbol(self) -> str:
        return self.replay.symbols[0]

    @property
    def fingerprint(self) -> str:
        document = {
            "replay_fingerprint": self.replay.fingerprint,
            "initial_cash": format(self.initial_cash, "f"),
            "commission_bps": format(self.commission_bps, "f"),
            "slippage_bps": format(self.slippage_bps, "f"),
            "max_volume_participation": format(
                self.max_volume_participation, "f"
            ),
            "annualization_periods": self.annualization_periods,
        }
        return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


@dataclass(frozen=True, slots=True)
class BacktestRunResult:
    config: BacktestConfig
    config_fingerprint: str
    strategy: StrategyDescriptor
    replay_result: ReplayRunResult
    signals: tuple[TargetPositionSignal, ...]
    orders: tuple[OrderRecord, ...]
    fills: tuple[Fill, ...]
    trades: tuple[ClosedTrade, ...]
    equity_curve: tuple[PortfolioSnapshot, ...]
    metrics: PerformanceMetrics
    run_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.config, BacktestConfig):
            raise BacktestConfigurationError(
                "Backtest result must contain a BacktestConfig."
            )
        _validate_sha256(self.config_fingerprint, "Config fingerprint")
        if self.config_fingerprint != self.config.fingerprint:
            raise BacktestInvariantError(
                "Backtest result config fingerprint does not match its config."
            )
        if not isinstance(self.strategy, StrategyDescriptor):
            raise BacktestConfigurationError(
                "Backtest result must contain a StrategyDescriptor."
            )
        if not isinstance(self.replay_result, ReplayRunResult):
            raise BacktestConfigurationError(
                "Backtest result must contain a ReplayRunResult."
            )
        if not isinstance(self.signals, tuple) or not all(
            isinstance(item, TargetPositionSignal) for item in self.signals
        ):
            raise BacktestConfigurationError(
                "Signals must be an immutable TargetPositionSignal tuple."
            )
        if not isinstance(self.orders, tuple) or not all(
            isinstance(item, OrderRecord) for item in self.orders
        ):
            raise BacktestConfigurationError(
                "Orders must be an immutable OrderRecord tuple."
            )
        if not isinstance(self.fills, tuple) or not all(
            isinstance(item, Fill) for item in self.fills
        ):
            raise BacktestConfigurationError(
                "Fills must be an immutable Fill tuple."
            )
        if not isinstance(self.trades, tuple) or not all(
            isinstance(item, ClosedTrade) for item in self.trades
        ):
            raise BacktestConfigurationError(
                "Trades must be an immutable ClosedTrade tuple."
            )
        if not isinstance(self.equity_curve, tuple) or not all(
            isinstance(item, PortfolioSnapshot) for item in self.equity_curve
        ):
            raise BacktestConfigurationError(
                "Equity curve must be an immutable PortfolioSnapshot tuple."
            )
        if not isinstance(self.metrics, PerformanceMetrics):
            raise BacktestConfigurationError(
                "Backtest result must contain PerformanceMetrics."
            )
        _validate_sha256(self.run_digest, "Run digest")
        if self.config.replay.fingerprint != self.replay_result.config_fingerprint:
            raise BacktestInvariantError(
                "Backtest and replay configuration fingerprints must match."
            )
        if len(self.signals) != self.replay_result.event_count:
            raise BacktestInvariantError(
                "Signal history must contain exactly one signal per replay event."
            )
        if len(self.equity_curve) != self.replay_result.event_count:
            raise BacktestInvariantError(
                "Equity curve must contain exactly one point per replay event."
            )


def backtest_config_document(config: BacktestConfig) -> dict[str, object]:
    replay = config.replay
    return {
        "symbol": config.symbol,
        "interval": replay.interval.value,
        "start": format_utc(replay.start) if replay.start is not None else None,
        "end": format_utc(replay.end) if replay.end is not None else None,
        "include_warnings": replay.include_warnings,
        "page_size": replay.page_size,
        "initial_cash": format(config.initial_cash, "f"),
        "commission_bps": format(config.commission_bps, "f"),
        "slippage_bps": format(config.slippage_bps, "f"),
        "max_volume_participation": format(
            config.max_volume_participation, "f"
        ),
        "annualization_periods": config.annualization_periods,
        "fingerprint": config.fingerprint,
    }


def signal_document(signal: TargetPositionSignal) -> dict[str, object]:
    return {
        "signal_id": signal.signal_id,
        "symbol": signal.symbol,
        "generated_at": format_utc(signal.generated_at),
        "target_fraction": format(signal.target_fraction, "f"),
        "reason": signal.reason,
    }


def order_document(record: OrderRecord) -> dict[str, object]:
    order = record.order
    return {
        "order_id": order.order_id,
        "signal_id": order.signal_id,
        "symbol": order.symbol,
        "side": order.side.value,
        "requested_at": format_utc(order.requested_at),
        "target_fraction": format(order.target_fraction, "f"),
        "reference_price": format(order.reference_price, "f"),
        "status": record.status.value,
        "requested_quantity": record.requested_quantity,
        "target_quantity": record.target_quantity,
        "quantity": record.quantity,
        "reject_reason": (
            record.reject_reason.value if record.reject_reason is not None else None
        ),
        "fill_id": record.fill_id,
    }


def fill_document(fill: Fill) -> dict[str, object]:
    return {
        "fill_id": fill.fill_id,
        "order_id": fill.order_id,
        "symbol": fill.symbol,
        "side": fill.side.value,
        "quantity": fill.quantity,
        "reference_price": format(fill.reference_price, "f"),
        "execution_price": format(fill.execution_price, "f"),
        "notional": format(fill.notional, "f"),
        "commission": format(fill.commission, "f"),
        "slippage_cost": format(fill.slippage_cost, "f"),
        "filled_at": format_utc(fill.filled_at),
    }


def snapshot_document(snapshot: PortfolioSnapshot) -> dict[str, object]:
    return {
        "timestamp": format_utc(snapshot.timestamp),
        "symbol": snapshot.symbol,
        "cash": format(snapshot.cash, "f"),
        "quantity": snapshot.quantity,
        "position_cost_basis": format(snapshot.position_cost_basis, "f"),
        "average_cost": format(snapshot.average_cost, "f"),
        "market_price": format(snapshot.market_price, "f"),
        "market_value": format(snapshot.market_value, "f"),
        "total_equity": format(snapshot.total_equity, "f"),
        "realized_pnl": format(snapshot.realized_pnl, "f"),
        "unrealized_pnl": format(snapshot.unrealized_pnl, "f"),
        "total_commission": format(snapshot.total_commission, "f"),
        "total_slippage": format(snapshot.total_slippage, "f"),
        "exposure": format(snapshot.exposure, "f"),
    }


def trade_document(trade: ClosedTrade) -> dict[str, object]:
    return {
        "trade_id": trade.trade_id,
        "symbol": trade.symbol,
        "entry_at": format_utc(trade.entry_at),
        "exit_at": format_utc(trade.exit_at),
        "bought_quantity": trade.bought_quantity,
        "sold_quantity": trade.sold_quantity,
        "entry_cost": format(trade.entry_cost, "f"),
        "exit_proceeds": format(trade.exit_proceeds, "f"),
        "net_pnl": format(trade.net_pnl, "f"),
        "return_fraction": format(trade.return_fraction, "f"),
    }


def metrics_document(metrics: PerformanceMetrics) -> dict[str, object]:
    return {
        "initial_equity": format(metrics.initial_equity, "f"),
        "final_equity": format(metrics.final_equity, "f"),
        "total_return": format(metrics.total_return, "f"),
        "cagr": _optional_float_text(metrics.cagr),
        "maximum_drawdown": format(metrics.maximum_drawdown, "f"),
        "annualized_volatility": _optional_float_text(
            metrics.annualized_volatility
        ),
        "sharpe_ratio": _optional_float_text(metrics.sharpe_ratio),
        "sortino_ratio": _optional_float_text(metrics.sortino_ratio),
        "calmar_ratio": _optional_float_text(metrics.calmar_ratio),
        "win_rate": _optional_decimal_text(metrics.win_rate),
        "profit_factor": _optional_decimal_text(metrics.profit_factor),
        "average_win": _optional_decimal_text(metrics.average_win),
        "average_loss": _optional_decimal_text(metrics.average_loss),
        "trade_count": metrics.trade_count,
        "turnover": format(metrics.turnover, "f"),
        "average_exposure": format(metrics.average_exposure, "f"),
        "commission_cost": format(metrics.commission_cost, "f"),
        "slippage_cost": format(metrics.slippage_cost, "f"),
        "benchmark_return": _optional_decimal_text(metrics.benchmark_return),
        "excess_return": _optional_decimal_text(metrics.excess_return),
    }


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _optional_float_text(value: float | None) -> str | None:
    return None if value is None else format(value, ".12g")


def _require_nonblank(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise BacktestConfigurationError(f"{field_name} cannot be blank.")


def _require_aware(value: object, field_name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise BacktestConfigurationError(
            f"{field_name} must include timezone information."
        )


def _require_finite_decimal(value: object, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise BacktestConfigurationError(f"{field_name} must be a finite Decimal.")


def _require_finite_positive_decimal(value: object, field_name: str) -> None:
    _require_finite_decimal(value, field_name)
    assert isinstance(value, Decimal)
    if value <= _ZERO:
        raise BacktestConfigurationError(f"{field_name} must be greater than zero.")


def _require_finite_nonnegative_decimal(value: object, field_name: str) -> None:
    _require_finite_decimal(value, field_name)
    assert isinstance(value, Decimal)
    if value < _ZERO:
        raise BacktestConfigurationError(f"{field_name} cannot be negative.")


def _require_finite_nonpositive_decimal(value: object, field_name: str) -> None:
    _require_finite_decimal(value, field_name)
    assert isinstance(value, Decimal)
    if value > _ZERO:
        raise BacktestConfigurationError(f"{field_name} cannot be positive.")


def _require_optional_finite_decimal(
    value: Decimal | None,
    field_name: str,
) -> None:
    if value is not None:
        _require_finite_decimal(value, field_name)


def _require_optional_finite_float(value: float | None, field_name: str) -> None:
    if value is not None and not math.isfinite(value):
        raise BacktestConfigurationError(f"{field_name} must be finite or None.")


def _require_nonnegative_int(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BacktestConfigurationError(
            f"{field_name} must be a nonnegative integer."
        )


def _require_optional_nonnegative_int(
    value: int | None,
    field_name: str,
) -> None:
    if value is not None:
        _require_nonnegative_int(value, field_name)


def _require_positive_int(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BacktestConfigurationError(f"{field_name} must be a positive integer.")


def _validate_uuid(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise BacktestConfigurationError(f"{field_name} must be a UUID string.")
    try:
        UUID(value)
    except (ValueError, AttributeError) as error:
        raise BacktestConfigurationError(
            f"{field_name} must be a valid UUID string."
        ) from error


def _validate_sha256(value: object, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise BacktestConfigurationError(
            f"{field_name} must be a lowercase SHA-256 digest."
        )
