# V-Lab Backtest Validation Audit

This document audits the parts of the Quantitative Research & Backtesting
System that are most relevant to a skeptical quantitative-research or
financial-risk reviewer. It deliberately prioritizes temporal integrity,
reproducibility, explicit assumptions, and failure handling over headline
backtest returns.

Synthetic validation remains a software/accounting test. A separate pinned SPY
experiment now demonstrates the same execution path on historical market data.
Neither is evidence of persistent alpha.

## A. Exact current test status

A clean GitHub Actions run on Ubuntu with Python 3.12 completed successfully.

Reference validation run including the historical experiment:
`37171307159`

- pytest: **534 passed**
- Ruff: **all checks passed**
- mypy: **no issues found in 255 source files**
- deterministic project gate: **18/18 stages completed**
- final status: **All project checks passed**
- canonical JSON summaries generated successfully
- two reproducible synthetic-validation SVG figures generated successfully
- workflow artifacts are uploaded immutably with a GitHub-recorded SHA-256
  digest for each run

The workflow performs a locked, non-editable installation, verifies imports
outside the repository, compiles source/tests, runs pytest/Ruff/mypy, exercises
fail-closed network behavior, and executes the deterministic data-quality,
replay, strategy-backtest, research-validity, point-in-time,
corporate-action, historical-dataset, robustness/walk-forward, and
statistical-validation simulations.

## B. Exact reproduced canonical experiment metrics

### Experiment definition

- purpose: software validation, not return maximization
- data source: deterministic synthetic candles generated in code
- symbol label: `005930`
- currency: KRW
- frequency: 1 day
- observations: **480**
- synthetic timestamps: 2024-01-01 through 2025-04-24 UTC
- strategy: SMA crossover, short window 20 / long window 80
- initial cash: **10,000,000 KRW**
- commission: **15 bps**
- slippage: **10 bps**
- maximum volume participation: **10%**
- execution: signal after current close, earliest fill at a later candle open
- benchmark: passive raw-price benchmark beginning at the first
  execution-eligible open
- live trading: disabled
- network access during replay: disabled

### Reproduced SMA results

These values are useful for checking accounting and reproducibility only.

- total return: **32.4509%**
- benchmark return: **42.2469%**
- excess return: **-9.7960%**
- maximum drawdown: **-8.0778%**
- annualized volatility: **2.6367%**
- Sharpe ratio: **5.622373**
- Sortino ratio: **11.277075**
- CAGR: **23.8996%**
- Calmar ratio: **2.958674**
- turnover: **3.812876**
- average exposure: **51.0856%**
- fills: **4**
- closed trades: **2**
- commission cost: **62,343.6015 KRW**
- slippage cost: **41,565.750 KRW**
- final equity: **13,245,090.6485 KRW**
- run digest:
  `7e6014be062fc437b83c21ec16c4dda500794a093744f239d584e57c94d52ddb`
- replay digest:
  `1a531d8409dcdf325fddcdc4d015dccc19961870d0600348195b8933a09219e7`

The deterministic buy-and-hold validation baseline reproduced a **41.8554%**
net total return and the same replay digest.

Machine-readable copies are committed under:

- `reports/validation/synthetic-sma-crossover.json`
- `reports/validation/synthetic-buy-and-hold.json`

The validation workflow also generates:

- `synthetic_cumulative_performance.svg`
- `synthetic_drawdown.svg`

Both figures explicitly label themselves as synthetic validation rather than
historical performance.

## B2. Pinned historical SPY experiment

The portfolio example downloads daily SPY OHLCV from the public
`quantstart/qstrader` repository at pinned commit
`e6d86a3ac3dc507b26e27b1f20c2949a69438ef7`. The exact downloaded bytes
reproduced SHA-256
`0968f804c2594ec6fd2d0624e4271b8e351169946260f31e32c6048189ec8098`.

The raw CSV is not vendored into this repository.

The protocol was frozen before the final holdout:

- SMA 20 / 100
- 15 bps commission
- 10 bps slippage
- 10% maximum volume participation
- development: 2000-01-03 through 2004-12-31
- validation: 2005-01-03 through 2006-12-29
- stress holdout: 2007-01-03 through 2009-12-31

No parameter grid was searched for this example.

Reproduced results:

| Period | SMA return | Passive price return | SMA max drawdown | Passive max drawdown |
| --- | ---: | ---: | ---: | ---: |
| Development | -3.76% | -16.00% | -25.41% | -49.14% |
| Validation | 7.93% | 17.27% | -5.61% | -7.59% |
| 2007-2009 holdout | 11.51% | -21.28% | -17.06% | -56.45% |

The validation underperformance is deliberately retained. The holdout result is
a stress-period observation, not a claim of general alpha.

The holdout SMA annualized volatility was **11.4509%** and Sharpe was
**0.374775**. Three completed round trips were all losing trades, while the
final open position remained marked to market and contributed to ending equity.
Two buy intents were rejected for insufficient cash after next-open movement
and modeled costs. This accounting detail is intentionally disclosed rather
than hidden behind the positive total-return number.

Committed historical evidence:

- `reports/historical-spy/README.md`
- `reports/historical-spy/holdout-sma-backtest.json`
- `reports/historical-spy/holdout-buy-and-hold.json`

Reference holdout run digest:
`ea3b776cef94bb256468ac957e056504dc16560ff600053ee845aad8e89caa3f`.

Important limitations: this is one ETF, raw OHLC price return rather than
dividend-adjusted total return, a public historical snapshot rather than an
institutional point-in-time feed, and a fixed-bps execution-cost model.

## C. Validated anti-lookahead safeguards

The backtest path enforces the following:

1. Replay records are ordered chronologically and duplicate/non-monotonic
   events fail closed.
2. A strategy observes a candle only when that replay event is delivered.
3. A signal must have `generated_at == event.event_time`.
4. The signal becomes a pending target-position order.
5. The order may execute only when a later candle arrives.
6. `NextOpenExecutionModel` rejects
   `candle.timestamp <= order.requested_at`.
7. The later candle's open is the execution reference price.
8. Point-in-time readers distinguish effective time from availability time and
   reject data or universe membership unavailable at the decision timestamp.
9. SMA state is updated incrementally from observed closes rather than by
   centered or full-sample rolling calculations.
10. The audit branch adds a **future-data mutation invariant**: changing prices
    strictly after a cutoff cannot change signals, order intents, fills, or
    equity state at or before that cutoff.

The clean validation run reproduced **0 detected look-ahead violations** in the
canonical synthetic experiment. That count is supplementary evidence; the
stronger evidence is the executable timing and mutation invariants.

## D. Validated overfitting and research-design safeguards

The repository implements and tests research-process controls rather than
merely describing them:

- non-overlapping train / validation / untouched-holdout windows
- immutable experiment specifications and SHA-256 research digests
- explicit parameter-search space, trial number, attempted-trial count, and
  selection metric
- one-time, fail-closed holdout consumption
- preservation of failed trials instead of silently dropping them
- frozen historical-dataset digest requirements
- chronological walk-forward folds
- nearby-parameter stress scenarios
- higher transaction-cost stress scenarios
- delayed-execution stress scenarios
- Probabilistic Sharpe Ratio
- Deflated Sharpe Ratio
- Bonferroni / false-discovery multiple-testing controls
- CSCV / Probability of Backtest Overfitting
- in-sample versus out-of-sample rank-stability checks

These controls reduce research-process risk. They do **not** prove economic
alpha or guarantee future performance.

### Risk controls: keep the layers separate

The **core historical strategy backtester** is intentionally simple. Its main
risk constraints are long-only/whole-share accounting, cash constraints,
volume-participation limits, deterministic costs, and explicit execution
semantics. It should not be described as having a sophisticated portfolio risk
overlay.

Separate later-stage components contain stronger controls:

- forward-shadow policy: minimum cash weight, maximum candidate weight,
  one-way turnover cap, evidence freshness checks, stale-feed fail-to-cash,
  and drawdown-triggered risk halt
- paper-execution gateway: maximum order quantity/notional, maximum position
  quantity, maximum open orders, minimum cash reserve, stale market/account
  rejection, limit-price deviation checks, kill switches, and reconciliation
  halts

Those controls should be described as forward/paper safety mechanisms, not as
features of every historical strategy backtest.

## E. Known limitations

- The core strategy backtester is single-symbol, long-only, and whole-share.
- Commission and slippage are transparent deterministic approximations, not a
  market-impact, queue-position, or full exchange-microstructure simulator.
- Risk ratios use a zero risk-free rate.
- Daily annualization defaults to 252 periods; intraday experiments require an
  explicit annualization convention.
- Drawdown and periodic risk metrics use replay snapshots rather than an
  intrabar mark-to-market path.
- The repository now reproduces one pinned historical SPY example, but it does
  not bundle the upstream raw CSV and does not claim an institutional
  historical market-data feed.
- The synthetic `005930` label must not be interpreted as Samsung Electronics
  historical market data.
- Corporate actions can be supplied through an explicit point-in-time
  timeline, but the project does not claim a complete broker-specific
  corporate-action feed.
- A corporate-action-aware run intentionally disables the simple raw-price
  passive benchmark where that comparison would be economically misleading.
- Live brokerage order submission is not part of the validated research
  workflow.

## F. Claims that are safe for a resume

Safe claims emphasize engineering and research integrity:

- built deterministic Python backtesting with close-to-next-open execution
- implemented point-in-time market-data and universe validation
- modeled transparent commission, slippage, and volume-participation effects
- added reproducible replay/config/run digests and machine-readable summaries
- enforced immutable train/validation/holdout research metadata and single-use
  holdouts
- implemented chronological walk-forward and robustness evaluation
- implemented statistical overfitting diagnostics including Deflated Sharpe
  and Probability of Backtest Overfitting
- validated the system in clean CI with **534 passing tests**
- added a future-data mutation invariant proving that post-cutoff observations
  cannot alter prior backtest state

## G. Claims that must NOT be made

Do not claim, from the current repository evidence alone:

- “generated 32.45% historical return”
- “generated historical alpha”
- “profitable trading strategy”
- “market-beating strategy”
- “historical Sharpe 5.62”
- “production-grade realistic execution”
- “institutional market-impact model”
- “live trading system”
- “survivorship-bias-free across all historical universes”
- “historical out-of-sample performance of X” unless a frozen historical
  dataset and exact experiment artifact are supplied and reproduced

The synthetic metrics above are deliberately excluded from resume performance
claims.

## H. Recommended V-Lab resume bullets

- Built a deterministic Python quantitative-research system with point-in-time
  market-data validation, close-to-next-open execution, commission/slippage
  accounting, and SHA-256 reproducibility; validated in clean CI with **534
  passing tests**.
- Implemented anti-leakage safeguards including monotonic replay,
  same-candle-execution rejection, data-availability checks, and a future-data
  mutation invariant proving post-cutoff observations cannot alter prior
  strategy state.
- Developed research-validity controls with immutable
  train/validation/holdout experiments, single-use holdouts, chronological
  walk-forward stress tests, Deflated Sharpe, and Probability of Backtest
  Overfitting diagnostics.

## I. Ten difficult technical interview questions and repository-grounded answers

### 1. How do you prevent using a closing price and filling at that same close?

The strategy observes the current candle close and timestamps its signal at
that event time. The order remains pending until replay delivers a later
candle. The execution model rejects any candle whose timestamp is not strictly
later than the order request and uses the later candle open as the execution
reference.

### 2. How do you test subtle future leakage rather than only checking timestamps?

The audit includes a future-data mutation test. Two runs have identical
history through a cutoff while prices strictly after the cutoff are changed
dramatically. Signals, order intents, fills, and equity state through the
cutoff must remain identical.

### 3. What does the transaction-cost model actually assume?

Commission is configurable basis points on executed notional. Slippage moves
the execution price adversely by configurable basis points from the later-open
reference. A deterministic volume-participation cap can limit the fill. This
is intentionally simpler than a market-impact or limit-order-book model.

### 4. What is deterministic about the system?

Normalized replay order is deterministic; strategy/configuration/replay inputs
are fingerprinted; order/fill identities are deterministic; final runs have a
digest; repeated identical simulations must reproduce the same digest.

### 5. How do you keep a holdout truly untouched?

Experiment metadata stores explicit train, validation, and holdout windows and
rejects overlap. Holdout consumption is persisted separately and can occur only
once. Concurrent attempts are tested to fail closed after the first successful
consumption.

### 6. How do you handle data that existed economically but was not known yet?

Point-in-time metadata separates an observation's effective timestamp from its
availability timestamp. A validated reader checks availability at the
simulated decision timestamp, preventing later-published information from
silently entering an earlier decision.

### 7. How are Sharpe and volatility calculated?

Periodic equity returns come from consecutive replay snapshots. Volatility
uses population standard deviation and the configured annualization factor.
Sharpe uses the same periodic series with a zero risk-free rate. The backtester
requires enough observations before reporting the risk ratios, and the audit
adds a manually checkable alternating-return unit test for the annualization
formula.

### 8. What is your defense against parameter-search overfitting?

The research layer records the declared search space, trial number, total
attempted trials, selection metric, and immutable data/code/cost assumptions.
Statistical validation then applies multiple-testing controls, Deflated
Sharpe, and CSCV/PBO instead of treating the best in-sample Sharpe as
trustworthy by default.

### 9. Why can the passive benchmark disappear in a corporate-action-aware run?

A raw start/end price comparison can become economically invalid around
splits, dividends, symbol changes, or delistings. The engine therefore avoids
claiming a simple benchmark result when an explicit corporate-action timeline
makes that raw comparison unreliable.

### 10. What is the most important next improvement?

The highest-value next improvement is not another strategy. It is stronger
historical-data provenance: an institutional-quality or explicitly
point-in-time market-data source with corporate-action completeness and a
separate exchange-open timestamp. That would reduce the largest remaining
evidence gap without increasing strategy complexity.
