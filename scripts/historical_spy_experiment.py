#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from html import escape
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from world_quant_system.backtest import (
    BacktestConfig,
    BuyAndHoldStrategy,
    SmaCrossoverStrategy,
    StrategyBacktestEngine,
)
from world_quant_system.backtest.reporting import (
    AtomicJsonBacktestSummaryWriter,
    backtest_summary_document,
)
from world_quant_system.backtest.simulation import InMemoryNormalizedCandleReader
from world_quant_system.data import NormalizedCandleRecord, QualityStatus
from world_quant_system.data.normalized_models import canonical_json_bytes
from world_quant_system.domain import Candle, CandleInterval
from world_quant_system.replay import ReplayConfig

SOURCE_COMMIT = "e6d86a3ac3dc507b26e27b1f20c2949a69438ef7"
SOURCE_URL = (
    "https://raw.githubusercontent.com/quantstart/qstrader/"
    f"{SOURCE_COMMIT}/data/SPY.csv"
)
SOURCE_LICENSE_URL = (
    "https://github.com/quantstart/qstrader/blob/"
    f"{SOURCE_COMMIT}/LICENSE"
)
SHORT_WINDOW = 20
LONG_WINDOW = 100
COMMISSION_BPS = Decimal("15")
SLIPPAGE_BPS = Decimal("10")
MAX_VOLUME_PARTICIPATION = Decimal("0.10")
INITIAL_CASH = Decimal("100000")
_NY = ZoneInfo("America/New_York")
_ITEM_NAMESPACE = UUID("4d175761-6978-5408-98d6-17b3ebd338a8")
_RAW_NAMESPACE = UUID("ed40e698-90b1-5ad2-bb72-7765b105507e")
_REPORT_NAMESPACE = UUID("4c149a5d-0256-53c5-b350-1e5a266b9b7d")


@dataclass(frozen=True, slots=True)
class ResearchWindow:
    name: str
    start: date
    end: date


WINDOWS = (
    ResearchWindow("development", date(2000, 1, 3), date(2004, 12, 31)),
    ResearchWindow("validation", date(2005, 1, 3), date(2006, 12, 29)),
    ResearchWindow("holdout", date(2007, 1, 3), date(2009, 12, 31)),
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run the pinned SPY historical backtest example used for the "
            "portfolio README."
        )
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports/historical-spy"),
    )
    args = parser.parse_args()
    asyncio.run(run(args.output_dir))


async def run(output_dir: Path) -> None:
    raw = _download()
    source_sha256 = hashlib.sha256(raw).hexdigest()
    records = _parse(raw)
    output_dir.mkdir(parents=True, exist_ok=True)

    period_results: dict[str, dict[str, object]] = {}
    holdout_strategy = None
    holdout_benchmark = None

    for window in WINDOWS:
        selected = tuple(
            record
            for record in records
            if window.start <= record.candle.timestamp.astimezone(_NY).date() <= window.end
        )
        if len(selected) < LONG_WINDOW + 20:
            raise RuntimeError(
                f"{window.name} window has too few observations: {len(selected)}"
            )

        strategy = await _run_backtest(
            selected,
            SmaCrossoverStrategy(
                short_window=SHORT_WINDOW,
                long_window=LONG_WINDOW,
            ),
        )
        benchmark = await _run_backtest(selected, BuyAndHoldStrategy())

        period_results[window.name] = {
            "window": {
                "start": window.start.isoformat(),
                "end": window.end.isoformat(),
            },
            "observations": len(selected),
            "strategy": backtest_summary_document(strategy),
            "buy_and_hold_price_return_baseline": backtest_summary_document(benchmark),
        }
        if window.name == "holdout":
            holdout_strategy = strategy
            holdout_benchmark = benchmark

    if holdout_strategy is None or holdout_benchmark is None:
        raise RuntimeError("Holdout result was not produced.")

    document = {
        "schema_version": 1,
        "purpose": (
            "Demonstrate the validated research pipeline on pinned historical "
            "market data; not evidence of persistent profitability."
        ),
        "source": {
            "upstream_repository": "quantstart/qstrader",
            "source_commit": SOURCE_COMMIT,
            "source_url": SOURCE_URL,
            "source_license_url": SOURCE_LICENSE_URL,
            "download_sha256": source_sha256,
            "raw_file_committed_here": False,
        },
        "dataset": {
            "symbol": "SPY",
            "frequency": "1d",
            "currency": "USD",
            "rows_in_upstream_file": len(records),
            "raw_columns_used": [
                "Date",
                "Open",
                "High",
                "Low",
                "Close",
                "Volume",
            ],
            "adjusted_close_used": False,
            "timestamp_semantics": (
                "Each daily bar is considered observable at 16:00 "
                "America/New_York on its trading date. A signal is generated "
                "only after that bar is observed. The next bar's Open is the "
                "economic execution reference; the stored fill timestamp "
                "identifies that next bar because the core daily-bar schema "
                "does not carry a separate exchange-open timestamp."
            ),
        },
        "protocol": {
            "strategy": "sma-crossover",
            "short_window": SHORT_WINDOW,
            "long_window": LONG_WINDOW,
            "parameter_selection": (
                "20/100 was frozen before this historical experiment and "
                "matches the repository's pre-existing documented SMA example; "
                "no grid search was performed for this example."
            ),
            "commission_bps": str(COMMISSION_BPS),
            "slippage_bps": str(SLIPPAGE_BPS),
            "max_volume_participation": str(MAX_VOLUME_PARTICIPATION),
            "initial_cash": str(INITIAL_CASH),
            "annualization_periods": 252,
            "development_window": {
                "start": WINDOWS[0].start.isoformat(),
                "end": WINDOWS[0].end.isoformat(),
            },
            "validation_window": {
                "start": WINDOWS[1].start.isoformat(),
                "end": WINDOWS[1].end.isoformat(),
            },
            "holdout_window": {
                "start": WINDOWS[2].start.isoformat(),
                "end": WINDOWS[2].end.isoformat(),
                "reason": (
                    "Chosen as a financial-stress evaluation period spanning "
                    "the 2007-2009 crisis and rebound, not because of the "
                    "strategy result."
                ),
            },
        },
        "limitations": [
            (
                "The source is a historical snapshot in an upstream public "
                "repository, not a contemporaneous institutional feed."
            ),
            (
                "Only one long-lived ETF is evaluated; this does not establish "
                "cross-sectional survivorship-bias control."
            ),
            (
                "Raw OHLC prices are used and cash dividends are not credited, "
                "so strategy and passive baseline are price-return comparisons, "
                "not total-return comparisons."
            ),
            (
                "The next-bar Open is used as the execution reference with a "
                "fixed basis-point slippage model; no market-impact or queue "
                "model is claimed."
            ),
            (
                "Daily bar timestamps represent close availability; the core "
                "bar schema does not separately timestamp the next session open."
            ),
        ],
        "periods": period_results,
    }

    experiment_path = output_dir / "historical-spy-experiment.json"
    experiment_path.write_bytes(canonical_json_bytes(document))
    AtomicJsonBacktestSummaryWriter(
        output_dir / "holdout-sma-backtest.json"
    ).write(holdout_strategy)
    AtomicJsonBacktestSummaryWriter(
        output_dir / "holdout-buy-and-hold.json"
    ).write(holdout_benchmark)

    _write_figure(
        output_dir / "holdout-cumulative-performance.svg",
        "Historical SPY holdout: cumulative price return",
        (
            ("SMA 20/100", _cumulative_returns(holdout_strategy)),
            ("Buy-and-hold price baseline", _cumulative_returns(holdout_benchmark)),
        ),
        y_label="Cumulative return (%)",
    )
    _write_figure(
        output_dir / "holdout-drawdown.svg",
        "Historical SPY holdout: replay-snapshot drawdown",
        (
            ("SMA 20/100", _drawdowns(holdout_strategy)),
            ("Buy-and-hold price baseline", _drawdowns(holdout_benchmark)),
        ),
        y_label="Drawdown (%)",
    )

    strategy_metrics = holdout_strategy.metrics
    benchmark_metrics = holdout_benchmark.metrics
    print(f"Historical source SHA-256: {source_sha256}")
    print(f"Historical rows parsed: {len(records)}")
    print(f"Holdout observations: {holdout_strategy.replay_result.event_count}")
    print(f"Holdout SMA total return: {strategy_metrics.total_return * 100:.4f}%")
    print(
        "Holdout passive price return: "
        f"{benchmark_metrics.total_return * 100:.4f}%"
    )
    print(
        "Holdout SMA maximum drawdown: "
        f"{strategy_metrics.maximum_drawdown * 100:.4f}%"
    )
    print(
        "Holdout passive maximum drawdown: "
        f"{benchmark_metrics.maximum_drawdown * 100:.4f}%"
    )
    print(
        "Holdout SMA annualized volatility: "
        f"{_optional_percent(strategy_metrics.annualized_volatility)}"
    )
    print(f"Holdout SMA Sharpe: {_optional_number(strategy_metrics.sharpe_ratio)}")
    print(f"Holdout SMA closed trades: {strategy_metrics.trade_count}")
    print(f"Holdout SMA run digest: {holdout_strategy.run_digest}")
    print(f"Historical experiment artifact: {experiment_path}")


def _download() -> bytes:
    request = Request(
        SOURCE_URL,
        headers={"User-Agent": "world-quant-system-historical-example/1.0"},
    )
    with urlopen(request, timeout=30) as response:
        payload = response.read()
    if not payload:
        raise RuntimeError("Historical source returned an empty payload.")
    return payload


def _parse(raw: bytes) -> tuple[NormalizedCandleRecord, ...]:
    text = raw.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    expected = {"Date", "Open", "High", "Low", "Close", "Volume", "Adj Close"}
    if set(reader.fieldnames or ()) != expected:
        raise RuntimeError(
            f"Unexpected historical CSV columns: {reader.fieldnames!r}"
        )

    parsed: list[NormalizedCandleRecord] = []
    seen_dates: set[date] = set()
    for row in reader:
        trading_date = date.fromisoformat(row["Date"])
        if trading_date in seen_dates:
            raise RuntimeError(f"Duplicate historical date: {trading_date}")
        seen_dates.add(trading_date)

        open_price = Decimal(row["Open"])
        high_price = Decimal(row["High"])
        low_price = Decimal(row["Low"])
        close_price = Decimal(row["Close"])
        volume = int(row["Volume"])
        if min(open_price, high_price, low_price, close_price) <= 0:
            raise RuntimeError(f"Non-positive price on {trading_date}")
        if low_price > min(open_price, close_price):
            raise RuntimeError(f"Low exceeds open/close on {trading_date}")
        if high_price < max(open_price, close_price):
            raise RuntimeError(f"High below open/close on {trading_date}")
        if volume < 0:
            raise RuntimeError(f"Negative volume on {trading_date}")

        observed_local = datetime.combine(
            trading_date,
            time(hour=16),
            tzinfo=_NY,
        )
        observed_at = observed_local.astimezone(UTC)
        candle = Candle(
            symbol="SPY",
            interval=CandleInterval.DAY_1,
            timestamp=observed_at,
            open_price=open_price,
            high_price=high_price,
            low_price=low_price,
            close_price=close_price,
            volume=volume,
            currency="USD",
            source="quantstart-qstrader-pinned",
        )
        source_document = {
            "date": row["Date"],
            "open": row["Open"],
            "high": row["High"],
            "low": row["Low"],
            "close": row["Close"],
            "volume": row["Volume"],
            "source_commit": SOURCE_COMMIT,
        }
        raw_sha = hashlib.sha256(
            canonical_json_bytes(source_document)
        ).hexdigest()
        identity = f"SPY|1d|{trading_date.isoformat()}|{SOURCE_COMMIT}"
        parsed.append(
            NormalizedCandleRecord(
                item_id=str(uuid5(_ITEM_NAMESPACE, identity)),
                candle=candle,
                quality_status=QualityStatus.PASS,
                raw_record_id=str(uuid5(_RAW_NAMESPACE, identity)),
                raw_content_sha256=raw_sha,
                quality_report_id=str(uuid5(_REPORT_NAMESPACE, identity)),
                normalized_at=observed_at + timedelta(minutes=1),
                normalizer_version="historical-example-1.0.0",
                content_sha256=raw_sha,
                schema_version=1,
                lineage_count=1,
            )
        )

    parsed.sort(key=lambda item: item.candle.timestamp)
    previous = None
    for item in parsed:
        if previous is not None and item.candle.timestamp <= previous:
            raise RuntimeError("Historical dates are not strictly increasing.")
        previous = item.candle.timestamp
    return tuple(parsed)


async def _run_backtest(
    records: tuple[NormalizedCandleRecord, ...],
    strategy: BuyAndHoldStrategy | SmaCrossoverStrategy,
):
    return await StrategyBacktestEngine(
        InMemoryNormalizedCandleReader(records),
        BacktestConfig(
            replay=ReplayConfig(
                symbols=("SPY",),
                interval=CandleInterval.DAY_1,
                page_size=128,
            ),
            initial_cash=INITIAL_CASH,
            commission_bps=COMMISSION_BPS,
            slippage_bps=SLIPPAGE_BPS,
            max_volume_participation=MAX_VOLUME_PARTICIPATION,
            annualization_periods=252,
        ),
        strategy,
    ).run()


def _cumulative_returns(result) -> tuple[float, ...]:
    initial = float(result.metrics.initial_equity)
    return tuple(
        (float(point.total_equity) / initial - 1.0) * 100.0
        for point in result.equity_curve
    )


def _drawdowns(result) -> tuple[float, ...]:
    peak = 0.0
    values: list[float] = []
    for point in result.equity_curve:
        equity = float(point.total_equity)
        peak = max(peak, equity)
        values.append(0.0 if peak == 0 else (equity / peak - 1.0) * 100.0)
    return tuple(values)


def _write_figure(
    path: Path,
    title: str,
    series: tuple[tuple[str, tuple[float, ...]], ...],
    *,
    y_label: str,
) -> None:
    width = 1100
    height = 620
    left, right, top, bottom = 95, 45, 110, 80
    plot_w = width - left - right
    plot_h = height - top - bottom
    count = len(series[0][1])
    if count < 2 or any(len(values) != count for _, values in series):
        raise RuntimeError("Historical figure series are not aligned.")

    all_values = [value for _, values in series for value in values]
    y_min = min(0.0, min(all_values))
    y_max = max(0.0, max(all_values))
    padding = max((y_max - y_min) * 0.08, 1.0)
    y_min -= padding
    y_max += padding

    def x(index: int) -> float:
        return left + plot_w * index / (count - 1)

    def y(value: float) -> float:
        return top + (y_max - value) / (y_max - y_min) * plot_h

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
        "<style>",
        "text{font-family:Arial,Helvetica,sans-serif;fill:#111}",
        ".title{font-size:26px;font-weight:700}",
        ".sub{font-size:14px;fill:#555}",
        ".tick{font-size:13px;fill:#444}",
        ".grid{stroke:#ddd;stroke-width:1}",
        ".axis{stroke:#222;stroke-width:1.5}",
        "</style>",
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{left}" y="42" class="title">{escape(title)}</text>',
        (
            f'<text x="{left}" y="70" class="sub">'
            "Pinned historical SPY price data; process demonstration, not "
            "evidence of persistent profitability.</text>"
        ),
    ]
    for tick in range(6):
        frac = tick / 5
        value = y_max - frac * (y_max - y_min)
        yy = top + frac * plot_h
        svg.append(
            f'<line x1="{left}" y1="{yy:.2f}" x2="{left + plot_w}" '
            f'y2="{yy:.2f}" class="grid"/>'
        )
        svg.append(
            f'<text x="{left - 10}" y="{yy + 5:.2f}" text-anchor="end" '
            f'class="tick">{value:.1f}</text>'
        )
    svg.extend(
        (
            f'<line x1="{left}" y1="{top}" x2="{left}" '
            f'y2="{top + plot_h}" class="axis"/>',
            f'<line x1="{left}" y1="{top + plot_h}" '
            f'x2="{left + plot_w}" y2="{top + plot_h}" class="axis"/>',
            (
                f'<text x="22" y="{top + plot_h / 2:.2f}" '
                f'transform="rotate(-90 22 {top + plot_h / 2:.2f})" '
                f'text-anchor="middle" class="tick">{escape(y_label)}</text>'
            ),
            (
                f'<text x="{left + plot_w / 2:.2f}" y="{height - 28}" '
                'text-anchor="middle" class="tick">Holdout replay event index</text>'
            ),
        )
    )
    styles = (("#111111", None), ("#666666", "10 6"))
    for (label, values), (stroke, dash) in zip(series, styles, strict=True):
        points = " ".join(
            f"{x(index):.2f},{y(value):.2f}"
            for index, value in enumerate(values)
        )
        dash_attr = "" if dash is None else f' stroke-dasharray="{dash}"'
        svg.append(
            f'<polyline points="{points}" fill="none" stroke="{stroke}" '
            f'stroke-width="3"{dash_attr}/>'
        )
    legend_y = top + 24
    for index, ((label, _), (stroke, dash)) in enumerate(
        zip(series, styles, strict=True)
    ):
        yy = legend_y + index * 28
        dash_attr = "" if dash is None else f' stroke-dasharray="{dash}"'
        svg.append(
            f'<line x1="{left + 18}" y1="{yy}" x2="{left + 62}" y2="{yy}" '
            f'stroke="{stroke}" stroke-width="3"{dash_attr}/>'
        )
        svg.append(
            f'<text x="{left + 72}" y="{yy + 5}" class="tick">'
            f"{escape(label)}</text>"
        )
    svg.append("</svg>")
    path.write_text("\n".join(svg) + "\n", encoding="utf-8")


def _optional_percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.4f}%"


def _optional_number(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.6f}"


if __name__ == "__main__":
    main()
