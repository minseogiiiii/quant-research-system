# V-Lab Backtest Validation Audit

This audit is scoped to the backtesting, data-integrity, and research-validity evidence that is relevant to a skeptical quantitative or financial-risk reviewer.

Validation commit: `874ae600c795111c85949e0677813397292df0f1`  
Clean GitHub Actions run: `37166979528`  
Validation command: `./scripts/check.sh`

## A. Exact current test status

The clean Ubuntu validation run completed successfully.

- pytest: **534 passed in 10.92s**
- Ruff: **all checks passed**
- mypy: **no issues found in 255 source files**
- full project gate: **18/18 validation stages completed**
- final status: **All project checks passed**

The CI workflow installs Python 3.12 and `uv` in a fresh runner, installs the locked project non-editably, verifies imports outside the repository, compiles source/tests, runs pytest, Ruff, mypy, and executes the deterministic simulation gates.

## B. Exact reproduced experiment metrics

The canonical strategy simulation is a **synthetic software-validation experiment**, not a historical alpha result.

- synthetic candles: **480**
- buy-and-hold total return: **41.8554%**
- SMA-crossover total return: **32.4509%**
- SMA closed trades: **2**
- deterministic backtest digest: **match**
- detected look-ahead violations: **0**
- transaction costs: **included**

These return values must not be presented as historical investment performance.

Additional reproduced integrity checks:

- normalized records: **2,000**
- replayed records: **2,000**
- deterministic replay digest: **match**
- data-quality simulation cases: **24,000**
- holdout reuse: **blocked**
- walk-forward simulation folds: **2**

## C. Validated anti-lookahead safeguards

The current backtest path enforces the following:

1. A strategy receives the current replay event and a portfolio snapshot marked at that candle close.
2. A generated signal must have `generated_at == event.event_time`.
3. The signal becomes a pending target-position order.
4. The order can execute only when a later candle arrives.
5. `NextOpenExecutionModel` rejects `candle.timestamp <= order.requested_at`.
6. Execution uses the later candle's open as the reference price.
7. The replay engine rejects non-monotonic or duplicate replay records.
8. Point-in-time readers validate that market data and universe metadata were available at the simulated decision time.
9. The audit branch adds a **future-data mutation invariant**: changing prices strictly after a cutoff cannot alter signals, order intents, fills, or equity state at or before that cutoff.

This is stronger evidence than a comment claiming “no look-ahead,” because the invariant is executable and part of the passing test suite.

## D. Validated overfitting safeguards

The repository contains implemented and tested research-validity mechanisms rather than only documentation:

- non-overlapping train / validation / untouched-holdout windows
- immutable experiment specifications and SHA-256 research digests
- explicit parameter-search metadata and attempted-trial counts
- one-time, fail-closed holdout consumption
- preserved failed trials rather than silent deletion
- frozen historical-dataset digest requirements
- chronological walk-forward folds
- nearby-parameter, higher-cost, and delayed-execution robustness scenarios
- Probabilistic Sharpe Ratio
- Deflated Sharpe Ratio
- multiple-testing controls
- CSCV / Probability of Backtest Overfitting
- in-sample vs out-of-sample rank-stability checks

These mechanisms reduce research-process risk. They do **not** prove that a strategy has economic alpha.

## E. Known limitations

- The v1 strategy backtester is single-symbol, long-only, and whole-share.
- Commission and slippage models are transparent deterministic approximations, not a full market-impact or queue-position simulator.
- Risk ratios use a zero risk-free rate.
- Daily annualization defaults to 252 periods; intraday runs require an explicit convention.
- Max drawdown and periodic risk metrics are based on replay snapshots, not an intrabar mark-to-market path.
- The repository does not bundle a production historical market dataset, so the repository alone does not reproduce a real historical-return claim.
- Corporate actions are supported through an explicit point-in-time timeline, but there is no claim of a complete broker-specific corporate-action feed.
- A corporate-action-aware run intentionally disables the simple raw-price passive benchmark because an unadjusted comparison can be misleading.
- There is no live broker order-submission path in the validated research workflow.

## F. Claims that are safe for a resume

Safe claims should emphasize engineering and research integrity:

- deterministic Python backtesting with close-to-next-open execution
- point-in-time data and universe validation
- transaction-cost and slippage accounting
- reproducible run/configuration digests
- immutable research metadata and single-use holdouts
- chronological robustness / walk-forward evaluation
- statistical overfitting diagnostics including DSR and PBO
- clean automated validation with 534 passing tests

## G. Claims that must NOT be made

Do not claim, based on the current repository evidence alone:

- “generated X% historical alpha”
- “profitable trading strategy”
- “market-beating strategy”
- “production-grade realistic execution”
- “institutional transaction-cost model”
- “live trading system”
- “survivorship-bias-free across all historical universes”
- “historical out-of-sample Sharpe of X” unless a frozen dataset and exact experiment artifact are supplied and reproduced
- synthetic 41.8554% or 32.4509% returns as investment performance

## H. Recommended V-Lab resume bullets

- Built a deterministic Python quantitative-research system with point-in-time market-data validation, close-to-next-open execution, commission/slippage accounting, and SHA-256 reproducibility; validated in clean CI with **534 passing tests**.
- Implemented anti-leakage safeguards including monotonic replay, same-candle execution rejection, data-availability checks, and a future-data mutation invariant proving that post-cutoff prices cannot alter prior strategy state.
- Developed overfitting controls with immutable train/validation/holdout experiments, one-time holdout use, chronological walk-forward stress tests, Deflated Sharpe Ratio, and Probability of Backtest Overfitting diagnostics.

## I. Ten technical interview questions and repository-grounded answers

### 1. How do you prevent using a closing price and filling at that same close?

The strategy observes the current candle close and timestamps its signal at that event time. The order remains pending until the replay engine delivers a later candle. The execution model rejects any candle whose timestamp is not strictly later than the signal timestamp and uses the later candle open as the execution reference.

### 2. How do you test for subtle future leakage rather than only checking timestamps?

The audit includes a future-data mutation test. Two runs have identical history through a cutoff, while prices strictly after the cutoff are changed dramatically. Signals, order intents, fills, and equity state through the cutoff must remain identical.

### 3. What does the transaction-cost model actually assume?

Commission is charged as configurable basis points on executed notional. Slippage moves the execution price adversely by configurable basis points from the next-open reference. A deterministic volume-participation cap can cause partial fills. This is intentionally simpler than a market-impact or limit-order-book model.

### 4. What is deterministic about the system?

Normalized replay order is deterministic; strategy/configuration/replay inputs are fingerprinted; order and fill identities are deterministic; final runs have a digest; repeated identical simulations are required to reproduce the same digest.

### 5. How do you keep a holdout truly untouched?

The experiment registry stores explicit train, validation, and holdout windows and rejects overlap. Holdout consumption is persisted separately and can occur only once; concurrent attempts are tested to fail closed after the first successful consumption.

### 6. How do you handle data that existed economically but was not known yet?

Point-in-time metadata distinguishes an observation's effective time from its availability time. A validated reader checks availability at the simulated decision timestamp, so later-published data cannot silently enter an earlier decision.

### 7. How are Sharpe and volatility calculated?

Periodic equity returns are computed from consecutive replay snapshots. Volatility uses population standard deviation and is annualized by the configured number of periods. Sharpe uses zero risk-free rate and the same annualization convention. The repository requires at least 20 periodic returns before reporting the risk ratios.

### 8. What is your defense against parameter-search overfitting?

The research layer records the declared search space, trial number, total attempted trials, selection metric, and immutable dataset/code/cost assumptions. Separate statistical validation implements multiple-testing controls, Deflated Sharpe, and CSCV/PBO instead of treating the best in-sample Sharpe as trustworthy by default.

### 9. Why is a simple benchmark disabled for some corporate-action runs?

A raw start/end price benchmark can become economically invalid when splits, dividends, symbol changes, or delistings occur. The engine therefore avoids claiming a simple benchmark result when an explicit corporate-action timeline is active rather than silently producing a misleading comparison.

### 10. What would you improve next?

The next evidence upgrade is not a more complicated strategy. It is a small, frozen, redistributable or downloadable historical dataset with a documented development/evaluation boundary, exact artifact digests, and reproducible recruiter-facing performance/drawdown figures. After that, execution realism can be expanded only where data supports the assumptions.
