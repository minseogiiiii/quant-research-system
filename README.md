# Quantitative Research & Backtesting System

A deterministic Python research system built to make historical strategy
evaluation auditable, with explicit controls for data availability, look-ahead
bias, execution timing, costs, overfitting, and reproducibility.

> Portfolio positioning: this project is presented as **Quantitative Research &
> Backtesting System**. The GitHub repository name `world-quant-system` is a
> legacy name and does not imply affiliation with WorldQuant.

## Key Verified Evidence

Fresh clean CI validation currently verifies:

- **534 passing tests**
- Ruff: **all checks passed**
- mypy: **no issues in 255 source files**
- deterministic replay and run digests
- same-candle/backward-time execution rejection
- a **future-data mutation invariant**: changing observations strictly after a
  cutoff cannot change earlier signals, order intents, fills, or equity state
- point-in-time availability checks for market data and universe membership
- immutable train/validation/holdout experiment metadata
- one-time, fail-closed holdout consumption
- chronological walk-forward robustness checks
- Deflated Sharpe, multiple-testing controls, and CSCV/PBO diagnostics
- a pinned **historical SPY experiment** reproduced in CI

The engineering claims above are stronger than any single strategy return.
This repository does not claim persistent profitability.

## Core Pipeline

```text
market data
→ immutable / point-in-time validation
→ deterministic replay
→ signal after current information is observable
→ next-period execution
→ commission / slippage / accounting
→ performance evaluation
→ walk-forward / robustness / overfitting checks
```

The core research path is under:

```text
src/world_quant_system/data/
src/world_quant_system/replay/
src/world_quant_system/backtest/
src/world_quant_system/research/
```

## Why the Backtest Can Be Audited

### 1. Signal and execution are separated

For the daily strategy backtester:

```text
bar t becomes observable
→ signal at t
→ pending target-position order
→ earliest economic execution reference = bar t+1 open
```

The execution model rejects a candle whose timestamp is not strictly later than
the order request. A strategy therefore cannot observe a close and fill at that
same close.

The daily-bar schema identifies the next bar as the fill event while using that
bar's Open as the economic execution price. It does not claim intraday
exchange-open timestamp precision.

### 2. Future-data mutation is an executable invariant

The test suite runs two histories that are identical through a cutoff and then
changes future prices dramatically. At or before the cutoff, the following must
remain identical:

- signals
- order intents
- fills
- portfolio/equity state

This catches classes of look-ahead leakage that a timestamp comment alone would
not detect.

### 3. Point-in-time eligibility fails closed

The research layer distinguishes when data is economically effective from when
it becomes available. Historical access is rejected when data or universe
membership was not available at the simulated decision time.

### 4. Costs and failed execution remain visible

Commission and adverse fixed-bps slippage are charged on executed notional.
Volume participation can restrict fills. Orders can be rejected, including
when a next-open gap plus costs makes the requested position unaffordable.
Rejected orders remain in the result rather than being silently resized after
the fact.

### 5. Results are reproducible

Replay events, strategy/configuration inputs, and final runs are fingerprinted.
Identical inputs are expected to reproduce identical digests.

## Historical Example: Pinned SPY Stress Evaluation

The repository now contains one deliberately simple historical example. Its
purpose is to demonstrate the pipeline on real market data, not to optimize a
strategy.

**Source**

- daily SPY OHLCV from the public `quantstart/qstrader` repository
- source commit:
  `e6d86a3ac3dc507b26e27b1f20c2949a69438ef7`
- downloaded bytes SHA-256:
  `0968f804c2594ec6fd2d0624e4271b8e351169946260f31e32c6048189ec8098`
- raw CSV is downloaded reproducibly and **not vendored here**

**Frozen protocol**

- SMA crossover: **20 / 100**
- commission: **15 bps**
- slippage: **10 bps**
- max volume participation: **10%**
- development: **2000-01-03 → 2004-12-31**
- validation: **2005-01-03 → 2006-12-29**
- stress holdout: **2007-01-03 → 2009-12-31**

The 20/100 parameters were frozen before this historical experiment and match a
pre-existing documented example in the repository. No parameter grid was
searched for this portfolio example.

| Period | SMA return | Passive price return | SMA max DD | Passive max DD |
| --- | ---: | ---: | ---: | ---: |
| Development | -3.76% | -16.00% | -25.41% | -49.14% |
| Validation | **7.93%** | **17.27%** | -5.61% | -7.59% |
| 2007–2009 holdout | **11.51%** | **-21.28%** | **-17.06%** | **-56.45%** |

The validation period is intentionally reported even though the strategy
**underperformed the passive baseline** there. In the stress holdout the
strategy had lower drawdown and higher ending equity, but one crisis-period
result is not evidence of general alpha.

The holdout also contains a useful accounting edge case: all three completed
round trips were losing trades, while a final open position remained marked to
market and contributed to ending equity. Two buy intents were rejected for
insufficient cash after next-open movement and modeled costs. These outcomes
are retained rather than cosmetically repaired.

Detailed protocol, caveats, and committed holdout JSON:

[`reports/historical-spy/`](reports/historical-spy/README.md)

Reproduce:

```bash
uv sync --locked
.venv/bin/python scripts/historical_spy_experiment.py \
  --output-dir reports/historical-spy
```

The script also generates at most two figures locally/CI: holdout cumulative
performance and holdout drawdown.

## Research Validity and Overfitting Controls

The research-validity layer records what was tested before results are treated
as evidence.

Implemented controls include:

- non-overlapping train / validation / untouched-holdout windows
- immutable experiment specifications and SHA-256 research digests
- declared parameter-search metadata and attempted-trial counts
- one-time holdout consumption
- retention of failed trials
- frozen historical-dataset digests
- chronological walk-forward folds
- nearby-parameter, higher-cost, and delayed-execution stress scenarios
- Probabilistic Sharpe Ratio
- Deflated Sharpe Ratio
- Bonferroni and false-discovery controls
- CSCV / Probability of Backtest Overfitting
- in-sample vs out-of-sample rank-stability checks

These are research-process safeguards, not a certificate that a strategy has
economic alpha.

## Synthetic Validation Is Separate

`scripts/strategy_backtest.py` uses deterministic synthetic candles to verify
execution timing, accounting, transaction costs, partial fills, metrics, and
reproducibility.

Synthetic returns and synthetic Sharpe are **software/accounting validation
only**. They are not historical performance and should not appear on a resume
as investment results.

Committed synthetic machine-readable summaries are under:

[`reports/validation/`](reports/validation/README.md)

## Reproducibility and CI

Set up the project:

```bash
uv python install 3.12
uv sync --locked
```

Run the full deterministic networkless validation gate:

```bash
./scripts/check.sh
```

The CI workflow then additionally runs the pinned historical SPY experiment and
uploads its machine-readable JSON and generated figures.

The validation gate includes clean non-editable installation, import checks
outside the repository, compilation, pytest, Ruff, mypy, fail-closed network
checks, and deterministic data/replay/backtest/research simulations.

## Scope and Limitations

The project is intentionally explicit about what it does **not** prove.

- Core historical backtesting is single-symbol, long-only, and whole-share.
- Commission and slippage are transparent fixed-bps approximations, not a
  market-impact or queue-position model.
- Daily risk metrics use 252-period annualization and a zero risk-free rate.
- Replay-snapshot drawdown is not an intrabar mark-to-market path.
- The SPY example uses raw OHLC price data and does not credit dividends, so its
  comparison is price-return rather than total-return.
- The historical example uses one long-lived ETF and does not establish
  cross-sectional survivorship-bias control.
- The public upstream data snapshot is not a contemporaneous institutional
  market-data feed.
- Corporate actions can be modeled through a separate point-in-time timeline,
  but the project does not claim a complete broker-specific corporate-action
  feed.
- No live-order submission is part of the validated research workflow.
- One historical experiment does not establish persistent profitability.

## Repository Structure

```text
src/world_quant_system/
├── data/          # raw capture, validation, normalized storage
├── replay/        # deterministic chronological replay
├── backtest/      # strategy, execution, portfolio, metrics
└── research/      # PIT data, validity, robustness, statistics

scripts/
├── check.sh
├── strategy_backtest.py
└── historical_spy_experiment.py

reports/
├── validation/       # synthetic software-validation evidence
└── historical-spy/   # pinned historical holdout evidence

docs/
├── VLAB_BACKTEST_AUDIT.md
└── TECHNICAL_REFERENCE.md
```

## Additional Systems Engineering

The repository also contains experiments around broker-neutral market-data
adapters, credential isolation, read-only certification, shadow portfolios,
paper execution, reconciliation, and disabled write transports.

Those modules are deliberately **secondary to the portfolio narrative**. They
show defensive systems engineering, but the primary project is the auditable
quantitative-research path above.

For detailed component documentation and CLI examples, see:

[`docs/TECHNICAL_REFERENCE.md`](docs/TECHNICAL_REFERENCE.md)

For the V-Lab-focused evidence review, safe claims, prohibited claims, and
technical interview questions, see:

[`docs/VLAB_BACKTEST_AUDIT.md`](docs/VLAB_BACKTEST_AUDIT.md)
