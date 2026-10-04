# Pinned Historical SPY Experiment

This example exists to demonstrate the validated backtesting pipeline on real
historical market data. It is **not** evidence of persistent profitability.

## Data

- Instrument: SPY
- Frequency: daily OHLCV
- Upstream: `quantstart/qstrader`
- Pinned upstream commit:
  `e6d86a3ac3dc507b26e27b1f20c2949a69438ef7`
- Downloaded source SHA-256:
  `0968f804c2594ec6fd2d0624e4271b8e351169946260f31e32c6048189ec8098`
- Upstream rows parsed: 4,026
- Raw CSV is **not** committed to this repository.
- Raw OHLC and Volume are used. `Adj Close` is intentionally not used by the
  execution engine.

The acquisition script downloads the file from the pinned upstream Git commit.
The upstream QSTrader repository is MIT-licensed, but this project still avoids
vendoring the raw market-data file.

## Frozen protocol

The parameters were not tuned for this experiment.

| Item | Value |
| --- | --- |
| Strategy | SMA crossover |
| Short / long window | 20 / 100 |
| Initial cash | $100,000 |
| Commission | 15 bps |
| Slippage | 10 bps |
| Max volume participation | 10% |
| Annualization | 252 periods |
| Development | 2000-01-03 to 2004-12-31 |
| Validation | 2005-01-03 to 2006-12-29 |
| Stress holdout | 2007-01-03 to 2009-12-31 |

The 20/100 parameters were frozen before this historical experiment and match
the repository's pre-existing documented SMA example. No parameter grid was
searched for this portfolio example.

## Results

These are price-return results because cash dividends are not credited.

| Period | SMA return | Passive price return | SMA max drawdown | Passive max drawdown | SMA Sharpe |
| --- | ---: | ---: | ---: | ---: | ---: |
| Development | -3.76% | -16.00% | -25.41% | -49.14% | -0.027 |
| Validation | 7.93% | 17.27% | -5.61% | -7.59% | 0.577 |
| Stress holdout | 11.51% | -21.28% | -17.06% | -56.45% | 0.375 |

The strategy **underperformed the passive price-return baseline during the
validation period**. The 2007-2009 holdout produced lower drawdown and a higher
ending equity than the passive price-return baseline, but that one stress
period is not evidence of general alpha.

A subtle accounting point matters in the holdout: the three completed round
trips were losing trades, while an open position remained marked to market at
the final observation. Open positions contribute to total equity and drawdown;
closed-trade statistics include completed round trips only. The engine does not
charge a hypothetical same-bar exit cost at the final observation.

Two buy intents were also rejected for insufficient cash after next-open price
movement and modeled costs. Those failures are retained rather than silently
altering position size after the fact.

## Timing semantics

Each daily bar is treated as observable at 16:00 America/New_York on its
trading date. The strategy can generate a signal only after that bar is
observed. Economic execution uses the **next bar's Open**.

The current daily-bar schema stores the next bar's event timestamp as the fill
timestamp; it does not separately store the exchange-open clock time. This is a
known representation limitation, not a claim of intraday timestamp precision.

## Reproduce

```bash
uv sync --locked
.venv/bin/python scripts/historical_spy_experiment.py \
  --output-dir reports/historical-spy
```

The script re-downloads the pinned source, fingerprints the exact bytes,
validates the OHLCV rows, runs the fixed protocol, writes machine-readable JSON,
and generates at most two SVG figures:

- `holdout-cumulative-performance.svg`
- `holdout-drawdown.svg`

The CI workflow runs this command after the full networkless project validation
gate.

Committed holdout summaries:

- `holdout-sma-backtest.json`
- `holdout-buy-and-hold.json`

Reference holdout SMA run digest:

`ea3b776cef94bb256468ac957e056504dc16560ff600053ee845aad8e89caa3f`

## Limitations

- The source is a historical snapshot in a public upstream repository, not a
  contemporaneous institutional feed.
- Only one long-lived ETF is evaluated; this is not cross-sectional
  survivorship-bias control.
- Dividends are omitted, so both strategy and passive comparison are
  price-return rather than total-return results.
- The cost model is fixed-bps and does not model market impact or queue
  position.
- The example does not establish persistent profitability or generalize beyond
  this instrument, protocol, and period.
