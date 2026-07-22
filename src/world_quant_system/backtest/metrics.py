from __future__ import annotations

import math
import statistics
from decimal import Decimal

from world_quant_system.backtest.models import (
    BacktestConfigurationError,
    ClosedTrade,
    PerformanceMetrics,
    PortfolioSnapshot,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_MIN_RISK_RETURN_OBSERVATIONS = 20
_MIN_CAGR_DAYS = 30.0


def calculate_performance_metrics(
    *,
    initial_equity: Decimal,
    equity_curve: tuple[PortfolioSnapshot, ...],
    trades: tuple[ClosedTrade, ...],
    turnover_notional: Decimal,
    annualization_periods: int,
    benchmark_start_price: Decimal | None,
    benchmark_end_price: Decimal | None,
) -> PerformanceMetrics:
    if (
        not isinstance(initial_equity, Decimal)
        or not initial_equity.is_finite()
        or initial_equity <= _ZERO
    ):
        raise BacktestConfigurationError(
            "Metrics initial equity must be a finite positive Decimal."
        )
    if isinstance(annualization_periods, bool) or annualization_periods <= 0:
        raise BacktestConfigurationError(
            "Annualization periods must be a positive integer."
        )

    final_equity = (
        initial_equity if not equity_curve else equity_curve[-1].total_equity
    )
    total_return = final_equity / initial_equity - _ONE
    maximum_drawdown = _maximum_drawdown(equity_curve)
    returns = _period_returns(equity_curve)
    annualized_volatility = _annualized_volatility(
        returns,
        annualization_periods,
    )
    sharpe_ratio = _sharpe_ratio(returns, annualization_periods)
    sortino_ratio = _sortino_ratio(returns, annualization_periods)
    cagr = _cagr(initial_equity, final_equity, equity_curve)
    calmar_ratio = (
        None
        if cagr is None or maximum_drawdown == _ZERO
        else cagr / abs(float(maximum_drawdown))
    )

    wins = tuple(trade.net_pnl for trade in trades if trade.net_pnl > _ZERO)
    losses = tuple(trade.net_pnl for trade in trades if trade.net_pnl < _ZERO)
    trade_count = len(trades)
    win_rate = None if trade_count == 0 else Decimal(len(wins)) / trade_count
    profit_factor = _profit_factor(wins, losses)
    average_win = None if not wins else sum(wins, _ZERO) / len(wins)
    average_loss = None if not losses else sum(losses, _ZERO) / len(losses)

    average_equity = (
        initial_equity
        if not equity_curve
        else sum((point.total_equity for point in equity_curve), _ZERO)
        / len(equity_curve)
    )
    turnover = _ZERO if average_equity == _ZERO else turnover_notional / average_equity
    average_exposure = (
        _ZERO
        if not equity_curve
        else sum((point.exposure for point in equity_curve), _ZERO)
        / len(equity_curve)
    )
    commission_cost = (
        _ZERO if not equity_curve else equity_curve[-1].total_commission
    )
    slippage_cost = _ZERO if not equity_curve else equity_curve[-1].total_slippage
    benchmark_return = _benchmark_return(
        benchmark_start_price,
        benchmark_end_price,
    )
    excess_return = (
        None if benchmark_return is None else total_return - benchmark_return
    )

    return PerformanceMetrics(
        initial_equity=initial_equity,
        final_equity=final_equity,
        total_return=total_return,
        cagr=cagr,
        maximum_drawdown=maximum_drawdown,
        annualized_volatility=annualized_volatility,
        sharpe_ratio=sharpe_ratio,
        sortino_ratio=sortino_ratio,
        calmar_ratio=calmar_ratio,
        win_rate=win_rate,
        profit_factor=profit_factor,
        average_win=average_win,
        average_loss=average_loss,
        trade_count=trade_count,
        turnover=turnover,
        average_exposure=average_exposure,
        commission_cost=commission_cost,
        slippage_cost=slippage_cost,
        benchmark_return=benchmark_return,
        excess_return=excess_return,
    )


def _maximum_drawdown(equity_curve: tuple[PortfolioSnapshot, ...]) -> Decimal:
    if not equity_curve:
        return _ZERO
    peak = equity_curve[0].total_equity
    maximum_drawdown = _ZERO
    for point in equity_curve:
        if point.total_equity > peak:
            peak = point.total_equity
        if peak == _ZERO:
            continue
        drawdown = point.total_equity / peak - _ONE
        if drawdown < maximum_drawdown:
            maximum_drawdown = drawdown
    return maximum_drawdown


def _period_returns(equity_curve: tuple[PortfolioSnapshot, ...]) -> tuple[float, ...]:
    if len(equity_curve) < 2:
        return ()
    returns: list[float] = []
    previous = equity_curve[0].total_equity
    for point in equity_curve[1:]:
        if previous <= _ZERO:
            previous = point.total_equity
            continue
        value = float(point.total_equity / previous - _ONE)
        if not math.isfinite(value):
            raise BacktestConfigurationError(
                "Equity curve produced a nonfinite periodic return."
            )
        returns.append(value)
        previous = point.total_equity
    return tuple(returns)


def _annualized_volatility(
    returns: tuple[float, ...],
    annualization_periods: int,
) -> float | None:
    if len(returns) < _MIN_RISK_RETURN_OBSERVATIONS:
        return None
    deviation = statistics.pstdev(returns)
    value = deviation * math.sqrt(annualization_periods)
    return value if math.isfinite(value) else None


def _sharpe_ratio(
    returns: tuple[float, ...],
    annualization_periods: int,
) -> float | None:
    if len(returns) < _MIN_RISK_RETURN_OBSERVATIONS:
        return None
    deviation = statistics.pstdev(returns)
    if deviation == 0:
        return None
    value = statistics.fmean(returns) / deviation * math.sqrt(annualization_periods)
    return value if math.isfinite(value) else None


def _sortino_ratio(
    returns: tuple[float, ...],
    annualization_periods: int,
) -> float | None:
    if len(returns) < _MIN_RISK_RETURN_OBSERVATIONS:
        return None
    downside_squares = [min(value, 0.0) ** 2 for value in returns]
    downside_deviation = math.sqrt(statistics.fmean(downside_squares))
    if downside_deviation == 0:
        return None
    value = (
        statistics.fmean(returns)
        / downside_deviation
        * math.sqrt(annualization_periods)
    )
    return value if math.isfinite(value) else None


def _cagr(
    initial_equity: Decimal,
    final_equity: Decimal,
    equity_curve: tuple[PortfolioSnapshot, ...],
) -> float | None:
    if len(equity_curve) < 2 or final_equity <= _ZERO:
        return None
    first = equity_curve[0].timestamp
    last = equity_curve[-1].timestamp
    elapsed_days = (last - first).total_seconds() / (24 * 60 * 60)
    if elapsed_days < _MIN_CAGR_DAYS:
        return None
    years = elapsed_days / 365.25
    growth_factor = float(final_equity / initial_equity)
    if not math.isfinite(growth_factor) or growth_factor <= 0:
        return None
    try:
        value = growth_factor ** (1.0 / years) - 1.0
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _profit_factor(
    wins: tuple[Decimal, ...],
    losses: tuple[Decimal, ...],
) -> Decimal | None:
    if not wins and not losses:
        return None
    gross_profit = sum(wins, _ZERO)
    gross_loss = abs(sum(losses, _ZERO))
    if gross_loss == _ZERO:
        return None
    return gross_profit / gross_loss


def _benchmark_return(
    start_price: Decimal | None,
    end_price: Decimal | None,
) -> Decimal | None:
    if start_price is None or end_price is None:
        return None
    if start_price <= _ZERO or end_price <= _ZERO:
        raise BacktestConfigurationError(
            "Benchmark prices must be positive when provided."
        )
    return end_price / start_price - _ONE
