#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Sequence

from world_quant_system.backtest.simulation import run_strategy_backtest_simulation


@dataclass(frozen=True, slots=True)
class _Series:
    label: str
    values: tuple[float, ...]
    stroke: str
    dash: str | None = None


def generate_validation_figures(output_dir: Path) -> tuple[Path, Path]:
    result = run_strategy_backtest_simulation()
    output_dir.mkdir(parents=True, exist_ok=True)

    buy_curve = result.buy_and_hold.equity_curve
    sma_curve = result.sma_crossover.equity_curve

    cumulative_path = output_dir / "synthetic_cumulative_performance.svg"
    drawdown_path = output_dir / "synthetic_drawdown.svg"

    _write_chart(
        cumulative_path,
        title="Synthetic validation: cumulative performance",
        subtitle=(
            "Software-validation candles only — not historical performance "
            "or evidence of alpha"
        ),
        y_label="Cumulative return (%)",
        series=(
            _Series(
                "Buy-and-hold validation baseline",
                _cumulative_returns(
                    buy_curve,
                    float(result.buy_and_hold.metrics.initial_equity),
                ),
                "#111111",
            ),
            _Series(
                "SMA crossover",
                _cumulative_returns(
                    sma_curve,
                    float(result.sma_crossover.metrics.initial_equity),
                ),
                "#666666",
                "10 6",
            ),
        ),
    )
    _write_chart(
        drawdown_path,
        title="Synthetic validation: drawdown",
        subtitle=(
            "Replay-snapshot drawdown on deterministic synthetic candles; "
            "not a historical risk estimate"
        ),
        y_label="Drawdown (%)",
        series=(
            _Series(
                "Buy-and-hold validation baseline",
                _drawdowns(buy_curve),
                "#111111",
            ),
            _Series(
                "SMA crossover",
                _drawdowns(sma_curve),
                "#666666",
                "10 6",
            ),
        ),
    )
    return cumulative_path, drawdown_path


def _cumulative_returns(
    curve: Sequence[object],
    initial_equity: float,
) -> tuple[float, ...]:
    if initial_equity <= 0:
        raise ValueError("Initial equity must be positive.")
    values: list[float] = []
    for point in curve:
        total_equity = float(getattr(point, "total_equity"))
        values.append((total_equity / initial_equity - 1.0) * 100.0)
    return tuple(values)


def _drawdowns(curve: Sequence[object]) -> tuple[float, ...]:
    values: list[float] = []
    peak: float | None = None
    for point in curve:
        equity = float(getattr(point, "total_equity"))
        peak = equity if peak is None else max(peak, equity)
        drawdown = 0.0 if peak == 0 else (equity / peak - 1.0) * 100.0
        values.append(drawdown)
    return tuple(values)


def _write_chart(
    path: Path,
    *,
    title: str,
    subtitle: str,
    y_label: str,
    series: tuple[_Series, ...],
) -> None:
    if not series or not all(item.values for item in series):
        raise ValueError("Chart series must be nonempty.")
    lengths = {len(item.values) for item in series}
    if len(lengths) != 1:
        raise ValueError("Chart series must have equal lengths.")

    width = 1200
    height = 700
    left = 105
    right = 55
    top = 125
    bottom = 95
    plot_width = width - left - right
    plot_height = height - top - bottom

    all_values = [value for item in series for value in item.values]
    y_min = min(min(all_values), 0.0)
    y_max = max(max(all_values), 0.0)
    if y_max == y_min:
        y_max += 1.0
        y_min -= 1.0
    padding = (y_max - y_min) * 0.08
    y_min -= padding
    y_max += padding

    count = next(iter(lengths))

    def x_coord(index: int) -> float:
        if count <= 1:
            return float(left)
        return left + plot_width * index / (count - 1)

    def y_coord(value: float) -> float:
        return top + (y_max - value) / (y_max - y_min) * plot_height

    svg: list[str] = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}">'
        ),
        "<style>",
        "text{font-family:Arial,Helvetica,sans-serif;fill:#111}",
        ".title{font-size:28px;font-weight:700}",
        ".subtitle{font-size:16px;fill:#555}",
        ".axis{stroke:#222;stroke-width:1.5}",
        ".grid{stroke:#ddd;stroke-width:1}",
        ".tick{font-size:14px;fill:#444}",
        ".legend{font-size:15px}",
        "</style>",
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{left}" y="45" class="title">{escape(title)}</text>',
        f'<text x="{left}" y="75" class="subtitle">{escape(subtitle)}</text>',
    ]

    for tick_index in range(6):
        fraction = tick_index / 5
        value = y_max - fraction * (y_max - y_min)
        y = top + fraction * plot_height
        svg.append(
            f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_width}" '
            f'y2="{y:.2f}" class="grid"/>'
        )
        svg.append(
            f'<text x="{left - 12}" y="{y + 5:.2f}" '
            f'text-anchor="end" class="tick">{value:.1f}</text>'
        )

    svg.extend(
        (
            f'<line x1="{left}" y1="{top}" x2="{left}" '
            f'y2="{top + plot_height}" class="axis"/>',
            f'<line x1="{left}" y1="{top + plot_height}" '
            f'x2="{left + plot_width}" y2="{top + plot_height}" class="axis"/>',
            (
                f'<text x="24" y="{top + plot_height / 2:.2f}" '
                f'transform="rotate(-90 24 {top + plot_height / 2:.2f})" '
                f'text-anchor="middle" class="tick">{escape(y_label)}</text>'
            ),
            (
                f'<text x="{left + plot_width / 2:.2f}" y="{height - 30}" '
                'text-anchor="middle" class="tick">Replay event index</text>'
            ),
        )
    )

    for item in series:
        points = " ".join(
            f"{x_coord(index):.2f},{y_coord(value):.2f}"
            for index, value in enumerate(item.values)
        )
        dash = "" if item.dash is None else f' stroke-dasharray="{item.dash}"'
        svg.append(
            f'<polyline points="{points}" fill="none" stroke="{item.stroke}" '
            f'stroke-width="3"{dash}/>'
        )

    legend_x = left + 20
    legend_y = top + 25
    for index, item in enumerate(series):
        y = legend_y + index * 30
        dash = "" if item.dash is None else f' stroke-dasharray="{item.dash}"'
        svg.append(
            f'<line x1="{legend_x}" y1="{y}" x2="{legend_x + 48}" y2="{y}" '
            f'stroke="{item.stroke}" stroke-width="3"{dash}/>'
        )
        svg.append(
            f'<text x="{legend_x + 60}" y="{y + 5}" '
            f'class="legend">{escape(item.label)}</text>'
        )

    svg.append("</svg>")
    path.write_text("\n".join(svg) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate synthetic recruiter-facing validation figures."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports/validation"),
    )
    arguments = parser.parse_args()
    paths = generate_validation_figures(arguments.output_dir)
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
