from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import NoReturn

from world_quant_system.backtest import (
    AtomicJsonBacktestSummaryWriter,
    BacktestConfig,
    BacktestConfigurationError,
    BacktestError,
    BacktestRunResult,
    BuyAndHoldStrategy,
    OrderStatus,
    SmaCrossoverStrategy,
    Strategy,
    StrategyBacktestEngine,
)
from world_quant_system.data import (
    NormalizedMarketDataError,
    SQLiteNormalizedMarketDataStore,
)
from world_quant_system.domain import CandleInterval
from world_quant_system.replay import ReplayConfig, ReplayError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wqs",
        description="Networkless research interface for World Quant System.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    backtest = subparsers.add_parser(
        "backtest",
        help="Run a deterministic long-only strategy backtest.",
    )
    backtest.add_argument("--normalized-root", type=Path, required=True)
    backtest.add_argument(
        "--strategy",
        choices=("buy-and-hold", "sma-cross"),
        required=True,
    )
    backtest.add_argument("--symbol", required=True)
    backtest.add_argument(
        "--interval",
        choices=tuple(interval.value for interval in CandleInterval),
        default=CandleInterval.DAY_1.value,
    )
    backtest.add_argument("--start")
    backtest.add_argument("--end")
    backtest.add_argument("--initial-cash", default="10000000")
    backtest.add_argument("--commission-bps", default="15")
    backtest.add_argument("--slippage-bps", default="10")
    backtest.add_argument("--max-volume-participation", default="0.10")
    backtest.add_argument(
        "--annualization-periods",
        type=int,
        help=(
            "Periods per year for annualized metrics. Defaults to 252 for 1d; "
            "required for intraday data."
        ),
    )
    backtest.add_argument("--short-window", type=int, default=20)
    backtest.add_argument("--long-window", type=int, default=100)
    backtest.add_argument("--page-size", type=int, default=1000)
    backtest.add_argument(
        "--pass-only",
        action="store_true",
        help="Exclude WARNING-quality normalized candles.",
    )
    backtest.add_argument(
        "--json-output",
        type=Path,
        help="Atomically write a compact result summary as JSON.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "backtest":
            result = asyncio.run(_run_backtest(arguments))
            print(_format_result(result))
            if arguments.json_output is not None:
                AtomicJsonBacktestSummaryWriter(arguments.json_output).write(result)
            return
    except (
        BacktestError,
        NormalizedMarketDataError,
        ReplayError,
        ValueError,
        OSError,
    ) as error:
        parser.exit(2, f"ERROR: {error}\n")
    _unreachable()


async def _run_backtest(arguments: argparse.Namespace) -> BacktestRunResult:
    root = arguments.normalized_root.expanduser().resolve()
    database = root / "normalized.sqlite3"
    if not database.is_file():
        raise BacktestConfigurationError(
            f"Normalized database does not exist: {database}"
        )
    strategy = _strategy_from_arguments(arguments)
    interval = CandleInterval(arguments.interval)
    replay = ReplayConfig(
        symbols=(arguments.symbol,),
        interval=interval,
        start=_parse_time(arguments.start, is_end=False),
        end=_parse_time(arguments.end, is_end=True),
        include_warnings=not arguments.pass_only,
        page_size=arguments.page_size,
    )
    config = BacktestConfig(
        replay=replay,
        initial_cash=_decimal(arguments.initial_cash, "initial cash"),
        commission_bps=_decimal(arguments.commission_bps, "commission bps"),
        slippage_bps=_decimal(arguments.slippage_bps, "slippage bps"),
        max_volume_participation=_decimal(
            arguments.max_volume_participation,
            "maximum volume participation",
        ),
        annualization_periods=_resolve_annualization_periods(
            interval,
            arguments.annualization_periods,
        ),
    )
    store = SQLiteNormalizedMarketDataStore(root)
    return await StrategyBacktestEngine(store, config, strategy).run()


def _strategy_from_arguments(arguments: argparse.Namespace) -> Strategy:
    if arguments.strategy == "buy-and-hold":
        return BuyAndHoldStrategy()
    return SmaCrossoverStrategy(
        short_window=arguments.short_window,
        long_window=arguments.long_window,
    )


def _resolve_annualization_periods(
    interval: CandleInterval,
    configured_value: int | None,
) -> int:
    if configured_value is not None:
        return configured_value
    if interval is CandleInterval.DAY_1:
        return 252
    raise BacktestConfigurationError(
        "Intraday backtests require --annualization-periods because market "
        "session lengths differ by venue."
    )


def _parse_time(value: str | None, *, is_end: bool) -> datetime | None:
    if value is None:
        return None
    try:
        if len(value) == 10:
            parsed_date = date.fromisoformat(value)
            boundary = time.max if is_end else time.min
            return datetime.combine(parsed_date, boundary, tzinfo=UTC)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise BacktestConfigurationError(
            f"Invalid ISO-8601 timestamp: {value}"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BacktestConfigurationError(
            "Backtest timestamps must include a timezone or use YYYY-MM-DD."
        )
    return parsed.astimezone(UTC)


def _decimal(value: str, field_name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise BacktestConfigurationError(
            f"Invalid decimal value for {field_name}: {value}"
        ) from error
    if not parsed.is_finite():
        raise BacktestConfigurationError(
            f"{field_name.capitalize()} must be finite."
        )
    return parsed


def _format_result(result: BacktestRunResult) -> str:
    metrics = result.metrics
    benchmark = (
        "N/A"
        if metrics.benchmark_return is None
        else _percent(metrics.benchmark_return)
    )
    replay = result.config.replay
    period_start = "unbounded" if replay.start is None else replay.start.isoformat()
    period_end = "unbounded" if replay.end is None else replay.end.isoformat()
    observed_start = (
        "N/A"
        if result.replay_result.first_event_at is None
        else result.replay_result.first_event_at.isoformat()
    )
    observed_end = (
        "N/A"
        if result.replay_result.last_event_at is None
        else result.replay_result.last_event_at.isoformat()
    )
    final_quantity = 0 if not result.equity_curve else result.equity_curve[-1].quantity
    filled_orders = sum(
        record.status is OrderStatus.FILLED for record in result.orders
    )
    partial_orders = sum(
        record.status is OrderStatus.PARTIALLY_FILLED for record in result.orders
    )
    rejected_orders = sum(
        record.status is OrderStatus.REJECTED for record in result.orders
    )
    expired_orders = sum(
        record.status is OrderStatus.EXPIRED for record in result.orders
    )
    lines = [
        "Execution mode: REPLAY",
        "Network access: DISABLED",
        "Live trading: DISABLED",
        "Order submission: DISABLED",
        "",
        f"Strategy:            {result.strategy.name}",
        f"Symbol:              {result.config.symbol}",
        f"Interval:            {replay.interval.value}",
        f"Requested period:    {period_start} -> {period_end}",
        f"Observed period:     {observed_start} -> {observed_end}",
        f"Events:              {result.replay_result.event_count}",
        f"PASS / WARNING:      {result.replay_result.pass_count} / "
        f"{result.replay_result.warning_count}",
        f"Commission (bps):    {result.config.commission_bps}",
        f"Slippage (bps):      {result.config.slippage_bps}",
        f"Initial equity:      {metrics.initial_equity:,.2f}",
        f"Final equity:        {metrics.final_equity:,.2f}",
        f"Total return:        {_percent(metrics.total_return)}",
        f"Benchmark return:    {benchmark}",
        f"Excess return:       {_optional_decimal_percent(metrics.excess_return)}",
        f"Maximum drawdown:    {_percent(metrics.maximum_drawdown)}",
        f"CAGR:                {_optional_percent(metrics.cagr)}",
        f"Annual volatility:   {_optional_percent(metrics.annualized_volatility)}",
        f"Sharpe ratio:        {_optional_number(metrics.sharpe_ratio)}",
        f"Sortino ratio:       {_optional_number(metrics.sortino_ratio)}",
        f"Calmar ratio:        {_optional_number(metrics.calmar_ratio)}",
        f"Orders:              {len(result.orders)}",
        f"Filled / partial:    {filled_orders} / {partial_orders}",
        f"Rejected / expired:  {rejected_orders} / {expired_orders}",
        f"Closed trades:       {metrics.trade_count}",
        f"Win rate:            {_optional_decimal_percent(metrics.win_rate)}",
        f"Profit factor:       {_optional_decimal_number(metrics.profit_factor)}",
        f"Average win:         {_optional_decimal_number(metrics.average_win)}",
        f"Average loss:        {_optional_decimal_number(metrics.average_loss)}",
        f"Turnover:            {metrics.turnover:.4f}",
        f"Average exposure:    {_percent(metrics.average_exposure)}",
        f"Final quantity:      {final_quantity}",
        f"Commission cost:     {metrics.commission_cost:,.2f}",
        f"Slippage cost:       {metrics.slippage_cost:,.2f}",
        f"Replay digest:       {result.replay_result.event_digest}",
        f"Config fingerprint:  {result.config_fingerprint}",
        f"Run digest:          {result.run_digest}",
    ]
    return "\n".join(lines)


def _percent(value: Decimal) -> str:
    return f"{value * Decimal('100'):.2f}%"


def _optional_percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.2f}%"


def _optional_number(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.4f}"


def _optional_decimal_percent(value: Decimal | None) -> str:
    return "N/A" if value is None else _percent(value)


def _optional_decimal_number(value: Decimal | None) -> str:
    return "N/A" if value is None else f"{value:,.4f}"


def _unreachable() -> NoReturn:
    raise AssertionError("Unreachable CLI command state.")


if __name__ == "__main__":
    main(sys.argv[1:])
