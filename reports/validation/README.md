# Synthetic Validation Artifacts

This directory contains machine-readable snapshots reproduced by the
deterministic synthetic strategy validation. They validate software behavior,
accounting, timing, and reproducibility; they are **not historical investment
performance**.

## Committed summaries

- `synthetic-sma-crossover.json`
- `synthetic-buy-and-hold.json`

The snapshots were reproduced in clean GitHub Actions validation run
`37167411823`. The same run produced validation artifact SHA-256
`06fc19db47f82b6ae556c1cbb8ed2bd79fdcf2943a64ae800adacdb1ed87e9ad`.

The canonical SMA simulation reproduced:

- 480 deterministic daily synthetic candles
- 20/80 SMA crossover
- 15 bps commission
- 10 bps slippage
- 10% maximum volume participation
- 32.4509% synthetic total return
- 42.2469% synthetic passive benchmark return
- -8.0778% maximum drawdown
- 2.6367% annualized volatility
- 5.622373 Sharpe ratio
- 3.812876 turnover
- 2 closed trades
- 0 detected look-ahead violations

These numbers must not be quoted as historical return, historical alpha, or a
forecast.

## Reproduce locally

After the repository environment is installed:

```bash
.venv/bin/python scripts/strategy_backtest.py --output-dir reports/validation
.venv/bin/python scripts/generate_validation_figures.py --output-dir reports/validation
```

The second command creates:

- `synthetic_cumulative_performance.svg`
- `synthetic_drawdown.svg`

Both generated figures visibly state that the underlying candles are synthetic.
The GitHub Actions validation workflow also uploads the JSON summaries and SVG
figures together as `synthetic-validation-artifacts`.

Run the complete clean validation gate with:

```bash
./scripts/check.sh
```

For the recruiter-facing interpretation, safe claims, limitations, and
interview questions, see `docs/VLAB_BACKTEST_AUDIT.md`.
