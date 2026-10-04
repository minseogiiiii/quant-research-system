import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from world_quant_system.backtest.metrics import calculate_performance_metrics
from world_quant_system.backtest.models import PortfolioSnapshot


def _point(index: int, equity: str, exposure: str = "0") -> PortfolioSnapshot:
    value = Decimal(equity)
    market_value = value * Decimal(exposure)
    cash = value - market_value
    quantity = 0 if market_value == 0 else 1
    market_price = Decimal("1") if quantity == 0 else market_value
    basis = Decimal("0") if quantity == 0 else market_value
    return PortfolioSnapshot(
        timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=index),
        symbol="ABC",
        cash=cash,
        quantity=quantity,
        position_cost_basis=basis,
        average_cost=basis,
        market_price=market_price,
        market_value=market_value,
        total_equity=value,
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        total_commission=Decimal("0"),
        total_slippage=Decimal("0"),
        exposure=Decimal(exposure),
    )


def test_metrics_compute_return_and_drawdown() -> None:
    curve = (_point(0, "100"), _point(1, "120"), _point(2, "90"), _point(3, "110"))
    metrics = calculate_performance_metrics(
        initial_equity=Decimal("100"),
        equity_curve=curve,
        trades=(),
        turnover_notional=Decimal("0"),
        annualization_periods=252,
        benchmark_start_price=Decimal("10"),
        benchmark_end_price=Decimal("11"),
    )
    assert metrics.total_return == Decimal("0.1")
    assert metrics.maximum_drawdown == Decimal("-0.25")
    assert metrics.benchmark_return == Decimal("0.1")
    assert metrics.excess_return == Decimal("0.0")
    assert metrics.trade_count == 0
    assert metrics.cagr is None
    assert metrics.annualized_volatility is None
    assert metrics.sharpe_ratio is None
    assert metrics.sortino_ratio is None


def test_metrics_risk_ratios_match_manual_periodic_return_check() -> None:
    equity = Decimal("100")
    points = [_point(0, "100")]
    for index in range(1, 21):
        periodic_return = Decimal("0.01") if index % 2 else Decimal("-0.01")
        equity *= Decimal("1") + periodic_return
        points.append(_point(index, format(equity, "f")))

    metrics = calculate_performance_metrics(
        initial_equity=Decimal("100"),
        equity_curve=tuple(points),
        trades=(),
        turnover_notional=Decimal("0"),
        annualization_periods=252,
        benchmark_start_price=None,
        benchmark_end_price=None,
    )

    expected_volatility = Decimal("0.01")
    annualized_expected = float(expected_volatility) * math.sqrt(252)

    assert metrics.annualized_volatility is not None
    assert math.isclose(
        metrics.annualized_volatility,
        annualized_expected,
        rel_tol=0,
        abs_tol=1e-12,
    )
    assert metrics.sharpe_ratio is not None
    assert math.isclose(metrics.sharpe_ratio, 0.0, rel_tol=0, abs_tol=1e-12)
    assert metrics.sortino_ratio is not None
    assert math.isclose(metrics.sortino_ratio, 0.0, rel_tol=0, abs_tol=1e-12)
