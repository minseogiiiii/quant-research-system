# World Quant System

Broker-neutral foundation for a quantitative trading system built around:

1. Research efficiency
2. Deterministic risk controls
3. Broker independence
4. Fail-closed integrations

The current project is intentionally limited to mock and networkless behavior.
It contains no live-order API, access token, brokerage credential, or real HTTP
transport.

## Current execution modes

- `mock`: deterministic in-memory data
- `replay`: deterministic normalized-candle playback foundation
- `shadow`: reserved for read-only broker data
- `live`: intentionally blocked

## Setup

```bash
uv python install 3.12
uv sync --locked
```

Do not modify `.venv` with `pip` or `uv pip`. Let the uv project workflow manage
it.

## Run

```bash
./scripts/run.sh
```

Expected safety indicators:

```text
Execution mode: MOCK
Broker provider: NONE
Live trading: DISABLED
```

## Validate everything

```bash
./scripts/check.sh
```

The run and check scripts use non-editable installation and force the local
project package to be reinstalled, preventing stale source code from remaining
inside `.venv`. The check script also verifies imports from outside the
repository, compiles source and tests, runs pytest, Ruff, mypy, validates the
network kill switch, and checks both application entry points.

## Current Toss adapter scope

The Toss adapter currently provides:

- request and response models with secret-safe representations
- typed, sanitized exception classes
- an injected transport protocol
- a default `NoNetworkTransport` that always refuses requests
- deterministic HTTP status-to-error mapping
- a networkless `TossTokenManager` with single-flight refresh
- optional managed Bearer authentication in `TossHttpClient`
- one controlled retry after a 401 response
- token-aware invalidation that cannot delete a newer concurrent token

There is no real HTTP transport, credential-bearing OAuth issuer, account
access, or order execution. All authentication integration tests use injected
fakes and make no network calls.

## Security rules

- Never commit `.env.local`, tokens, account identifiers, or brokerage secrets.
- Never give an LLM access to live brokerage credentials.
- Keep default integrations fail-closed.
- Do not add order submission before deterministic risk gates and account
  reconciliation exist.

## Recover a damaged environment

When `.venv` or editable installation metadata is corrupted, use:

```bash
./scripts/reset_env.sh
```

Do not delete individual files inside `.venv` and do not run `uv pip install`
inside this managed project environment.

## Networkless OAuth token manager

The Toss adapter now includes a networkless `TossTokenManager` foundation.
It deliberately does not know the client ID, client secret, or OAuth endpoint.
A future issuer adapter must be injected explicitly.

Safety properties:

- Default construction fails closed and cannot contact the network.
- Concurrent callers share a single token issuance attempt.
- Tokens refresh before expiry using a monotonic clock.
- A failed refresh never falls back to a token that may have been invalidated.
- Access-token representations are masked.
- No refresh-token workflow is implemented because the official Toss API does
  not provide refresh tokens; expired tokens are reissued through the token
  endpoint.

The test suite includes a deterministic seven-day token lifecycle simulation.
This is a software state-machine simulation, not an investment-strategy
backtest and not evidence of trading profitability.

## Networkless Toss market-data provider

The Toss adapter includes a read-only `TossMarketDataProvider` implemented
against the official prices and candles contracts:

- `GET /api/v1/prices` for one or multiple current prices
- `GET /api/v1/candles` for `1m` and `1d` OHLCV pages
- at most 200 symbols or candles per request
- deterministic symbol normalization and request ordering
- timezone-aware timestamps with configurable future-skew rejection
- strict positive finite prices and nonnegative integer volume
- OHLC range validation, duplicate detection, monotonic ordering checks,
  currency consistency, pagination validation, and missing-result detection
- oldest-to-newest candle output for research and replay consumers
- transparent propagation of authentication, rate-limit, and server errors

The provider only consumes an injected `TossHttpClient`. The default HTTP
transport and default token issuer still fail closed, so this repository makes
no real broker request and contains no credential-bearing code.

## Immutable raw market-data archive

The project now includes an opt-in `FileRawMarketDataStore`. When it is injected
into `TossMarketDataProvider`, each successful prices or candles response is
archived **before** parsed models are returned. If configured storage fails, the
provider fails closed rather than returning market data that was not archived.

The archive is intentionally local and broker-neutral:

- append-only gzip-compressed canonical JSON documents
- UTC date partitioning under `data/raw/blobs/<provider>/YYYY/MM/DD/`
- SQLite catalog with WAL and full synchronous durability
- atomic same-filesystem writes and interrupted-write recovery
- SHA-256 integrity verification on every read
- request-ID idempotency when Toss provides `X-Request-Id`
- cross-thread and cross-process writer serialization on macOS/Linux
- async APIs that move blocking disk work off the event loop
- strict size limits and JSON type validation
- redaction of sensitive request parameters and headers
- rejection of token-, credential-, and account-like response fields
- no order, account, credential, or live-network functionality

Example wiring for a future read-only collector:

```python
from world_quant_system.adapters.toss import TossMarketDataProvider
from world_quant_system.data import FileRawMarketDataStore

raw_store = FileRawMarketDataStore("data/raw")
provider = TossMarketDataProvider(
    client,
    raw_recorder=raw_store,
)
```

The current archive preserves the complete parsed JSON response, response text,
sanitized headers, request parameters, timestamps, and checksums. It does not
claim byte-for-byte preservation of the original HTTP wire encoding because a
real transport has not been added yet. `data/` remains excluded from Git.


## Data quality gate and quarantine index

The read-only market-data path now supports an opt-in `MarketDataQualityGate`.
It verifies the immutable raw record before assessing parsed quotes or candles,
persists a deterministic report, and blocks only `QUARANTINE` results. `WARNING`
results remain available to research consumers so legitimate volatility and
other possible alpha are not silently discarded.

Statuses are deliberately conservative:

- `PASS`: no quality issue was detected.
- `WARNING`: usable for research, but the report records conditions such as a
  stale quote, unusual candle gap, extreme return, wide range, zero-volume run,
  or flatline run.
- `QUARANTINE`: unusable. Examples include raw-file integrity failure, metadata
  mismatch, endpoint mismatch, strict parse failure, future timestamp, or
  source mismatch.

Quality reports are stored separately from raw market data:

```text
data/
├── raw/                  # immutable gzip JSON and raw catalog
└── quality/
    └── quality.sqlite3   # durable PASS/WARNING/QUARANTINE reports
```

The quality catalog uses SQLite WAL mode, full synchronous durability,
connection closing after every operation, canonical JSON, SHA-256 verification,
and a semantic fingerprint. Temporal checks are anchored to the raw capture
time, so reprocessing the same immutable record later remains deterministic.
The assessment key includes the raw record ID, raw content digest, dataset kind,
validator version, and a fingerprint of the active quality policy. Repeated
assessment is idempotent, while a policy change creates a distinct assessment
instead of silently reusing old results.

Example wiring:

```python
from world_quant_system.adapters.toss import TossMarketDataProvider
from world_quant_system.data import (
    FileRawMarketDataStore,
    MarketDataQualityGate,
    SQLiteDataQualityStore,
)

raw_store = FileRawMarketDataStore("data/raw")
quality_store = SQLiteDataQualityStore("data/quality")
quality_gate = MarketDataQualityGate(raw_store, quality_store)

provider = TossMarketDataProvider(
    client,
    raw_recorder=raw_store,
    quality_gate=quality_gate,
)
```

The quality thresholds are policy inputs, not trading signals. They can be
changed only through an explicit `DataQualityPolicy`; the defaults preserve
large legitimate price moves as warnings rather than quarantining them.

Run the deterministic data-quality simulation with:

```bash
.venv/bin/python scripts/quality_gate_backtest.py
```

The simulation covers clean quotes and candles, empty responses, stale and
future timestamps, source mismatches, extreme moves, wide ranges, candle gaps,
zero-volume runs, and flatline runs. It checks exact classifications, unsafe-
case recall, false quarantines, and preservation of potentially valuable
high-volatility observations. This is a data-quality state-machine backtest,
not an investment-strategy profitability backtest.

## Versioned normalized storage and deterministic replay

Quality-approved data can now be written to an opt-in
`SQLiteNormalizedMarketDataStore`. The normalized catalog is separate from the
immutable raw archive and quality reports:

```text
data/
├── raw/
├── quality/
└── normalized/
    └── normalized.sqlite3
```

The storage path is designed for reproducible research rather than order
execution:

- only `PASS` and `WARNING` datasets can enter normalized storage
- `QUARANTINE` data is rejected before any write
- Decimal prices and timezone-aware UTC event times are preserved exactly
- deterministic item IDs make repeated writes idempotent
- a conflicting rewrite of the same source/symbol/interval/timestamp is blocked
- every item retains raw-record, raw SHA-256, quality-report, quality-status,
  normalizer-version, and normalization-time lineage
- multiple equivalent source observations add lineage instead of duplicating
  the canonical candle
- any warning lineage makes the canonical item conservatively `WARNING`
- canonical JSON and SHA-256 protect catalog contents from silent corruption
- SQLite WAL, `synchronous=FULL`, bounded queries, batch transactions, and
  connection closing support durable concurrent use
- candle scans are cursor-paged in deterministic order by timestamp, symbol,
  and item ID

`MarketDataNormalizer` can be connected to `TossMarketDataProvider` only when a
quality gate is also configured. When enabled, raw capture, quality assessment,
and normalization all complete before data is returned to the caller.

The `DeterministicReplayEngine` reads only normalized candles. It has no broker
transport and no order API. It advances a monotonic replay clock, never emits
the same item twice, does not move backward in time, and produces a SHA-256
event digest. `WARNING` data is included by default to avoid deleting possible
alpha events, but a PASS-only replay can be requested explicitly.

Run the normalized-storage and replay integrity simulation with:

```bash
.venv/bin/python scripts/normalized_replay_backtest.py
```

The simulation checks 2,000 synthetic normalized candles, deterministic replay
across different page boundaries, warning-event preservation, PASS-only
filtering, idempotent duplicate writes, conflicting rewrite rejection, and
quarantine rejection. This is a storage and replay integrity backtest. It does
not estimate strategy returns, Sharpe ratio, or future profitability. Those
metrics require historical market data, transaction-cost assumptions, and
the separate strategy backtesting engine documented below.

## Strategy Backtesting Engine v1

The research layer now includes a deterministic, single-symbol, long-only
strategy backtester. It consumes only `NormalizedCandleRecord` objects through
the existing replay interface and has no broker transport, credential access,
or order-submission path.

Safety and accounting rules:

- a signal is generated only after the current candle close is observed
- the earliest possible fill is the next candle open
- same-candle and backward-time fills are rejected
- prices, cash, cost basis, commissions, and slippage use `Decimal`
- cash and long position quantities cannot become negative
- duplicate fills and multiple fills for the same v1 order are blocked
- fees and adverse fixed-basis-point slippage are included in PnL
- volume participation limits are deterministic and can produce partial fills
- strategy, replay, configuration, and final run digests support reproduction
- each engine is single-use and each strategy is reset before a run, preventing state leakage

Included reference strategies:

- `buy-and-hold`: observes the first close, then requests entry for the next open
- `sma-crossover`: O(1) rolling short/long simple moving averages, with a
  long-or-flat target and no access to future candles

Included performance measures:

- total return and CAGR
- maximum drawdown
- annualized volatility, Sharpe, Sortino, and Calmar ratios
  (risk ratios require at least 20 period returns; CAGR requires 30 days)
- closed-trade win rate, profit factor, average win, and average loss
- turnover and average exposure
- commission and slippage costs
- a passive benchmark beginning at the first execution-eligible open

Run the deterministic synthetic accounting and look-ahead simulation with:

```bash
.venv/bin/python scripts/strategy_backtest.py
```

This simulation validates software behavior with synthetic candles. Its returns
are not historical investment results and do not predict future profitability.

Backtest v1 uses a zero risk-free rate, whole-share long-only orders, generic
basis-point fees, and deterministic fixed slippage. Exchange taxes, tick-size
rounding, dividends, splits, delistings, and other corporate actions must be
handled by later market-specific models or by correctly adjusted input data.
Open positions contribute to total return and drawdown; closed-trade statistics
include completed round trips only. A position still open at the final candle is
marked at that close and is not charged a hypothetical same-candle exit cost.

### Research CLI

The separate `wqs` command keeps research functions away from the default mock
application entry point. It reads an existing normalized database and remains
networkless:

```bash
wqs backtest \
  --normalized-root data/normalized \
  --strategy sma-cross \
  --symbol 005930 \
  --interval 1d \
  --start 2020-01-01 \
  --end 2025-12-31 \
  --initial-cash 10000000 \
  --short-window 20 \
  --long-window 100 \
  --commission-bps 15 \
  --slippage-bps 10 \
  --max-volume-participation 0.10
```

Daily CLI runs default to 252 annualization periods. Intraday runs must provide
`--annualization-periods` explicitly because session lengths differ by market.

Use `--pass-only` to exclude `WARNING` observations. By default, warnings remain
available so high-volatility events are not silently removed from research.
`--json-output PATH` atomically writes a compact summary. The CLI cannot enable
LIVE mode or submit an order.

## Research Validity Layer v1

The research-validity layer records what was tested before results are used for
strategy selection. It is deliberately separate from broker adapters and from
the backtest execution engine. Its purpose is to reduce hidden data snooping,
accidental holdout reuse, and irreproducible parameter searches.

The first release provides:

- non-overlapping half-open train, validation, and untouched-holdout windows
- deterministic experiment IDs derived from strategy, parameters, data digest,
  Git commit, costs, execution assumptions, split, and search audit
- a durable SQLite experiment registry with canonical JSON and SHA-256 checks
- idempotent concurrent registration of the same experiment specification
- immutable success or failure outcomes, including preservation of failed runs
- parent-experiment and change-reason lineage for strategy revisions
- parameter-search metadata: search ID, search space, trial number, total trials,
  and selection metric
- fail-closed, one-time holdout consumption, including concurrent-use protection
- a networkless CLI with no broker transport or order-submission path

The registry is stored separately from normalized market data:

```text
data/
├── normalized/
│   └── normalized.sqlite3
└── research/
    └── research.sqlite3
```

Research windows use `[start, end)` semantics. Adjacent periods are valid, but
any overlap is rejected. A holdout can be recorded as consumed only once for an
experiment. Retrying that operation fails closed, even if the same digest is
submitted again.

Register an experiment before running or selecting it:

```bash
wqs research register \
  --root data/research \
  --strategy sma-cross \
  --strategy-version 1.0.0 \
  --dataset-digest <lowercase-sha256> \
  --code-commit <git-commit> \
  --parameters-json '{"short_window":20,"long_window":100}' \
  --cost-model-json '{"commission_bps":"15","slippage_bps":"10"}' \
  --execution-model-json '{"fill":"next_open"}' \
  --train-start 2020-01-01 \
  --train-end 2023-01-01 \
  --validation-start 2023-01-01 \
  --validation-end 2024-01-01 \
  --holdout-start 2024-01-01 \
  --holdout-end 2025-01-01 \
  --search-id sma-grid-v1 \
  --search-space-json '{"short_window":[10,20],"long_window":[80,100]}' \
  --trial-number 1 \
  --total-trials 4 \
  --selection-metric validation_sharpe
```

Record either a successful result digest or a failed experiment reason:

```bash
wqs research record-outcome \
  --root data/research \
  --experiment-id <uuid> \
  --status succeeded \
  --result-digest <lowercase-sha256>
```

```bash
wqs research record-outcome \
  --root data/research \
  --experiment-id <uuid> \
  --status failed \
  --failure-reason 'validation robustness gate failed'
```

Inspect the immutable record and its holdout state:

```bash
wqs research inspect \
  --root data/research \
  --experiment-id <uuid>
```

Record the single permitted untouched-holdout evaluation:

```bash
wqs research consume-holdout \
  --root data/research \
  --experiment-id <uuid> \
  --result-digest <lowercase-sha256>
```

Run the deterministic registry simulation with:

```bash
.venv/bin/python scripts/research_validity_simulation.py
```

Point-in-time universes and explicit delisting metadata are implemented in the
next section. Deflated Sharpe, PBO, robustness matrices, walk-forward
evaluation, and full corporate-action accounting remain later validity phases.

## Point-in-Time Data Integrity v1

The point-in-time catalog prevents a historical research run from silently
using securities, universe membership, or data that were unavailable at the
simulated decision time. It remains networkless and has no broker, credential,
or order-submission path.

The first release stores immutable, SHA-256-protected records for:

- security listing, tradability, and delisting boundaries
- point-in-time universe membership intervals and their publication times
- per-item `effective_at` and `available_at` timestamps
- explicit delisting events, reasons, and last-tradable timestamps
- deterministic universe snapshots and single-symbol backtest contexts

The distinction between timestamps is mandatory:

- `effective_at` is the time represented by the data or membership
- `available_at` is the first time a historical decision could have used it

A record can be effective while still unavailable. Such a record is rejected
until the decision timestamp reaches `available_at`. Membership intervals use
half-open `[member_from, member_until)` semantics and cannot overlap for the
same universe and security.

The catalog is stored separately from normalized observations and experiment
records:

```text
data/
├── normalized/
│   └── normalized.sqlite3
├── research/
│   └── research.sqlite3
└── point_in_time/
    └── point_in_time.sqlite3
```

Register a security lifecycle before any membership or data item:

```bash
wqs point-in-time register-security \
  --root data/point_in_time \
  --exchange XKRX \
  --symbol 005930 \
  --listed-at 1975-06-11 \
  --tradable-from 1975-06-11 \
  --source exchange-master-v1 \
  --source-digest <lowercase-sha256>
```

Register a historically effective universe interval and when that record was
known:

```bash
wqs point-in-time register-membership \
  --root data/point_in_time \
  --universe-id KOSPI \
  --exchange XKRX \
  --symbol 005930 \
  --member-from 2020-01-01 \
  --member-until 2025-01-01 \
  --available-at 2019-12-31T09:00:00+09:00 \
  --source universe-history-v1 \
  --source-digest <lowercase-sha256>
```

Every normalized candle used by point-in-time replay requires its own
availability record. `--data-id` is the normalized candle `item_id`:

```bash
wqs point-in-time register-availability \
  --root data/point_in_time \
  --data-id <normalized-item-uuid> \
  --data-kind candle \
  --exchange XKRX \
  --symbol 005930 \
  --effective-at 2020-01-02T15:30:00+09:00 \
  --available-at 2020-01-02T15:30:00+09:00 \
  --source historical-candles-v1 \
  --source-digest <lowercase-sha256>
```

Delisted securities remain in historical universes. A lifecycle with a
`delisted_at` boundary is unusable until an explicit matching delisting record
is stored:

```bash
wqs point-in-time register-delisting \
  --root data/point_in_time \
  --exchange XKRX \
  --symbol 000001 \
  --last-tradable-at 2021-06-30 \
  --delisted-at 2021-07-01 \
  --available-at 2021-06-01 \
  --reason regulatory \
  --source delisting-history-v1 \
  --source-digest <lowercase-sha256>
```

Build a deterministic universe snapshot or a bounded backtest context:

```bash
wqs point-in-time snapshot \
  --root data/point_in_time \
  --universe-id KOSPI \
  --as-of 2020-06-01
```

```bash
wqs point-in-time backtest-context \
  --root data/point_in_time \
  --universe-id KOSPI \
  --exchange XKRX \
  --symbol 005930 \
  --start 2020-01-01 \
  --end 2025-01-01
```

Historical backtests can enable fail-closed validation by providing all three
point-in-time arguments together. Explicit start and end boundaries are then
required:

```bash
wqs backtest \
  --normalized-root data/normalized \
  --point-in-time-root data/point_in_time \
  --universe-id KOSPI \
  --exchange XKRX \
  --strategy sma-cross \
  --symbol 005930 \
  --start 2020-01-01 \
  --end 2024-12-31
```

The validated context digest is included in the backtest configuration
fingerprint. Each replay candle is checked for:

- a tradable security lifecycle at the event timestamp
- exactly one effective universe membership
- membership availability no later than the decision timestamp
- a matching candle availability record
- `available_at <= decision_at`
- explicit delisting metadata whenever the lifecycle is delisted

The experiment registry can also include a point-in-time context digest and
canonical policy JSON, so strategy selection records identify the exact data
eligibility context used by the backtest.

Run the deterministic integration simulation with:

```bash
.venv/bin/python scripts/point_in_time_simulation.py
```

The simulation verifies insertion-order-independent snapshot and context
digests, future-data rejection, membership-overlap rejection, and preservation
of a historically valid security that was later delisted.

The next section adds separate market-event accounting for splits, cash
dividends, symbol changes, and explicit delisting economics. Raw prices remain
immutable; mergers, spin-offs, and multi-asset consideration remain later
phases.

## Corporate Actions & Delisting Economics v1

The research backtester now applies historically knowable corporate actions to
portfolio accounting without rewriting raw or normalized OHLCV observations.
Corporate-action metadata lives in a separate immutable SQLite catalog:

```text
data/
├── normalized/
│   └── normalized.sqlite3
├── point_in_time/
│   └── point_in_time.sqlite3
└── corporate_actions/
    └── corporate_actions.sqlite3
```

The first release supports:

- forward splits and reverse splits
- deterministic fractional-share rejection or explicit cash in lieu
- cash-dividend entitlement at the ex timestamp and cash credit at payment
- a separate, digest-pinned flat-rate dividend withholding model
- symbol changes that preserve the open position and cost basis
- delisting settlement by explicit cash price or recovery rate
- zero-value delisting as an explicit full-loss outcome

Raw and normalized prices are never overwritten. The event timeline is stored
and applied separately, and the backtest result includes every application,
its before/after quantity and cost basis, cash movement, tax, and reference
price.

Register a two-for-one split:

```bash
wqs corporate-actions register \
  --root data/corporate_actions \
  --exchange XKRX \
  --symbol 005930 \
  --type split \
  --effective-at 2020-05-04T00:00:00+09:00 \
  --available-at 2020-04-01T09:00:00+09:00 \
  --source exchange-actions-v1 \
  --source-digest <lowercase-sha256> \
  --ratio-numerator 2 \
  --ratio-denominator 1
```

A reverse split that can create fractional shares must either use the default
fail-closed `reject` policy or provide an explicit cash-in-lieu price and run
with the `cash_in_lieu` policy.

Register a cash dividend. The effective timestamp must equal the ex timestamp,
and declaration, ex, record, and payment times must be ordered:

```bash
wqs corporate-actions register \
  --root data/corporate_actions \
  --exchange XKRX \
  --symbol 005930 \
  --type cash_dividend \
  --effective-at 2024-03-28T00:00:00+09:00 \
  --available-at 2024-01-31T09:00:00+09:00 \
  --declared-at 2024-01-31T09:00:00+09:00 \
  --ex-at 2024-03-28T00:00:00+09:00 \
  --record-at 2024-03-29T00:00:00+09:00 \
  --payment-at 2024-04-19T00:00:00+09:00 \
  --cash-amount-per-share 361 \
  --source exchange-actions-v1 \
  --source-digest <lowercase-sha256>
```

Dividend rights are fixed using the held quantity at the ex event. Selling
before payment does not erase the entitlement. Net dividend proceeds are also
attributed to the entitled trade so trade-level win rate and profit-factor
metrics do not silently omit dividends paid after sale.

Register a symbol change:

```bash
wqs corporate-actions register \
  --root data/corporate_actions \
  --exchange XNAS \
  --symbol OLD \
  --type symbol_change \
  --effective-at 2023-06-01T00:00:00-04:00 \
  --available-at 2023-05-01T09:00:00-04:00 \
  --new-symbol NEW \
  --source exchange-actions-v1 \
  --source-digest <lowercase-sha256>
```

Register explicit delisting economics. Exactly one settlement method is
required; missing settlement information is rejected rather than pretending
the position was sold at the last normal close:

```bash
wqs corporate-actions register \
  --root data/corporate_actions \
  --exchange XNAS \
  --symbol NEW \
  --type delisting \
  --effective-at 2024-01-15T00:00:00-05:00 \
  --available-at 2024-01-05T09:00:00-05:00 \
  --delisting-cash-price 0 \
  --source delisting-economics-v1 \
  --source-digest <lowercase-sha256>
```

Inspect or list immutable records and build a digest-pinned context:

```bash
wqs corporate-actions inspect \
  --root data/corporate_actions \
  --action-id <uuid>
```

```bash
wqs corporate-actions list \
  --root data/corporate_actions \
  --exchange XKRX \
  --symbol 005930 \
  --start 2020-01-01 \
  --end 2025-01-01
```

```bash
wqs corporate-actions backtest-context \
  --root data/corporate_actions \
  --exchange XKRX \
  --symbol 005930 \
  --start 2020-01-01 \
  --end 2025-01-01
```

Run a corporate-action-aware backtest:

```bash
wqs backtest \
  --normalized-root data/normalized \
  --corporate-action-root data/corporate_actions \
  --exchange XKRX \
  --strategy buy-and-hold \
  --symbol 005930 \
  --start 2020-01-01 \
  --end 2024-12-31 \
  --dividend-tax-rate 0.154 \
  --fractional-share-policy reject
```

For a symbol-change path, replay can contain the predecessor and successor
symbols only when they are pinned by the same corporate-action context. When
point-in-time validation is also enabled, each symbol segment receives its own
bounded lifecycle, universe-membership, and availability context. A pending
order is cancelled at symbol-change and delisting boundaries rather than being
silently filled against a different security identity.

Corporate-action runs include these values in deterministic configuration and
research fingerprints:

- corporate-action context digest
- corporate-action dataset and policy digests
- dividend-tax model digest
- point-in-time context digest when enabled
- every corporate-action application in the final run digest

Cash dividends that cross a requested backtest boundary are rejected in v1.
This avoids omitting a pre-start entitlement or ending with an unvalued
receivable. Mergers, spin-offs, stock dividends, rights offerings, dividend
reinvestment, and multi-asset consideration remain later phases.

Run the deterministic economics simulation with:

```bash
.venv/bin/python scripts/corporate_action_simulation.py
```

It verifies insertion-order-independent context digests, deterministic
backtest digests, idempotent writes, conflict rejection, future-known action
rejection, split basis preservation, after-sale dividend attribution,
symbol continuity, and explicit zero-value delisting loss. The simulation and
CLI remain networkless; live trading and broker order submission stay disabled.

## Historical Dataset Integrity & Ingestion v1

The research system can now turn a strict local CSV file into an immutable,
reproducible historical dataset snapshot without using the network. Import is
routed through the existing raw archive, quality gate, normalizer, and
normalized SQLite store rather than bypassing earlier safety layers.

Required CSV columns, in this exact order, are:

```text
timestamp,symbol,open,high,low,close,volume,currency
```

Timestamps must be ISO-8601 values with an explicit UTC offset matching the
configured IANA timezone. Rows must be strictly increasing, contain one symbol
and currency, and satisfy the existing `Candle` and quality-gate invariants.
For daily data, expected weekdays and explicit holidays are used to detect
missing sessions. The default policy rejects a missing session; `warn` retains
an immutable warning in the manifest.

Each dataset manifest pins:

- provider, exchange, symbol, interval, timezone, and currency
- source-file SHA-256 and normalized-data digest
- item count and exact UTC time range
- quality policy and final quality status
- code commit
- point-in-time context digest
- corporate-action context digest
- deterministic dataset ID and dataset digest

The catalog stores raw, quality, and normalized data below a dataset-specific
root and verifies all three layers before returning a snapshot:

```text
data/historical_datasets/
├── historical_datasets.sqlite3
├── manifests/
└── datasets/
    └── <dataset-id>/
        ├── raw/
        ├── quality/
        └── normalized/
```

Import a networkless CSV:

```bash
wqs dataset import \
  --root data/historical_datasets \
  --source-file samsung.csv \
  --provider localcsv \
  --exchange KRX \
  --symbol 005930 \
  --interval 1d \
  --timezone Asia/Seoul \
  --currency KRW \
  --code-commit <git-commit> \
  --point-in-time-context-digest <sha256> \
  --corporate-action-context-digest <sha256> \
  --holiday 2020-01-01
```

Verify and freeze the imported snapshot before research use:

```bash
wqs dataset validate \
  --root data/historical_datasets \
  --dataset-id <uuid>

wqs dataset freeze \
  --root data/historical_datasets \
  --dataset-id <uuid>
```

Inspection and listing remain networkless:

```bash
wqs dataset inspect --root data/historical_datasets --dataset-id <uuid>
wqs dataset list --root data/historical_datasets
```

A benchmark link can be created only after both datasets are frozen and their
interval, UTC range, timezone, currency, and exact normalized event timestamps
match:

```bash
wqs dataset link-benchmark \
  --root data/historical_datasets \
  --dataset-id <strategy-dataset-uuid> \
  --benchmark-dataset-id <benchmark-dataset-uuid>
```

The deterministic simulation is available at:

```bash
.venv/bin/python scripts/historical_dataset_simulation.py
```

This release does not download data, estimate future profitability, or enable
broker orders. Exchange calendars beyond weekday-plus-holiday validation,
provider-specific acquisition adapters, large multi-symbol files, and
walk-forward robustness analysis remain later phases.

## Robustness & Walk-Forward Validation v1

The research layer includes a deterministic robustness harness for frozen
historical datasets. It does not search for or promise profitable strategies.
It tests whether an existing strategy survives chronological out-of-sample
windows, nearby parameters, higher costs, and delayed execution assumptions.

Core rules:

- every run requires a frozen historical dataset digest
- train, validation, and test windows are explicit half-open intervals
- test windows must advance strictly through time
- all strategy variants and stress scenarios run in deterministic order
- SMA parameter perturbations retain every valid neighboring combination
- commission and slippage assumptions can be stressed together
- execution delay is implemented by delaying observed targets without exposing
  future candles
- reports emphasize median and worst test returns, worst drawdown,
  out-of-sample degradation, regime results, and pass rate
- report identity excludes creation time, so identical research inputs produce
  the same report digest
- the CLI remains networkless and cannot submit broker orders

Example:

```bash
wqs robustness \
  --normalized-root data/normalized \
  --symbol 005930 \
  --historical-dataset-digest <sha256> \
  --fold '2020-01-01,2022-01-01,2022-01-01,2023-01-01,2023-01-01,2024-01-01' \
  --strategy sma-cross \
  --base-short-window 20 \
  --base-long-window 100 \
  --short-offset -2 --short-offset 0 --short-offset 2 \
  --long-offset -10 --long-offset 0 --long-offset 10 \
  --cost-multiplier 1 --cost-multiplier 1.5 --cost-multiplier 2 \
  --execution-delay 1 --execution-delay 2 \
  --json-output data/research/robustness.json
```

The resulting pass or failure is a research-policy decision, not evidence of
future returns. A strategy that fails remains recorded rather than being
silently discarded.

## Statistical Validation & Overfitting Control v1

This layer evaluates whether a completed strategy search is likely to contain
selection bias or backtest overfitting. It does not optimize strategies and it
must not be used as an objective function for selecting new parameters.

Input is a strict, networkless wide CSV with one timestamp column and one
periodic-return column per successful trial:

```text
timestamp,<trial-id-1>,<trial-id-2>,...
2020-01-01T00:00:00Z,0.001,-0.002,...
```

All timestamps must be timezone-aware and strictly increasing. Every successful
trial must have one finite return at every timestamp. Periodic returns must be
greater than -1. Failed trials can be supplied explicitly or loaded from the
immutable experiment registry. Declared trials with neither returns nor a
recorded failure are counted as omitted, generate a warning, and force the
report to fail.

The report includes:

- raw period and annualized Sharpe ratios
- sample skewness and Pearson kurtosis
- Probabilistic Sharpe Ratio against zero
- Bonferroni family-wise error control
- Benjamini-Hochberg false-discovery q-values
- Deflated Sharpe Ratio using declared/effective trial count and cross-trial
  Sharpe variance
- combinatorially symmetric cross-validation (CSCV)
- Probability of Backtest Overfitting (PBO)
- probability that the in-sample-selected trial loses out of sample
- in-sample versus out-of-sample rank correlation and rank-reversal detection
- deterministic report ID and SHA-256 digest

The CSCV implementation uses an even number of equal, contiguous time
partitions. The observation count must be divisible by the configured partition
count, each partition must contain at least four observations, and every
in-sample combination is paired with its complement out of sample. PBO is the
fraction of combinations where the in-sample winner ranks at or below the
out-of-sample median.

Example without a registry:

```bash
wqs statistical-validation \
  --returns-csv data/research/search_returns.csv \
  --dataset-digest <sha256> \
  --search-id sma-grid-v2 \
  --attempted-trials 81 \
  --failed-trial 'trial-17=insufficient observations' \
  --effective-trials 40 \
  --cscv-partitions 8 \
  --json-output data/research/statistical-validation.json
```

For stronger completeness guarantees, use experiment UUIDs as CSV column names
and connect the registry:

```bash
wqs statistical-validation \
  --returns-csv data/research/search_returns.csv \
  --dataset-digest <sha256> \
  --search-id sma-grid-v2 \
  --registry-root data/research/registry \
  --json-output data/research/statistical-validation.json
```

Registry mode requires every registered trial in the search to have an outcome.
Successful experiment IDs must exactly match the CSV columns; failed outcomes
are preserved in the report. The statistical report fails when registry
coverage is incomplete, when declared trials are omitted, or when required DSR,
PBO, rank-stability, Bonferroni, or false-discovery gates fail.

Run the deterministic simulation with:

```bash
.venv/bin/python scripts/statistical_validation_simulation.py
```

The methodology follows Bailey and López de Prado's Deflated Sharpe Ratio and
Bailey, Borwein, López de Prado, and Zhu's CSCV/PBO framework. The report remains
an estimate whose quality depends on complete trial disclosure, representative
historical data, and defensible assumptions about the effective number of
independent trials. It does not establish future profitability or authorize
live trading.
## Research-to-Portfolio Promotion & Risk Engine v1

This networkless research layer evaluates whether individually validated strategy
candidates can be combined into a sufficiently diversified, cost-aware portfolio.
It does not generate alpha, authorize live trading, connect to a broker, or submit
orders.

### Required evidence

The engine requires two aligned local inputs:

1. A candidate evidence manifest bound to one frozen historical-dataset digest.
2. A wide CSV of periodic net out-of-sample returns using the same timestamps and
   comparable cost/execution assumptions for every candidate.

The return file uses:

```text
timestamp,<candidate-id-1>,<candidate-id-2>,...
2022-01-03T00:00:00Z,0.0012,-0.0004,...
```

Timestamps must be timezone-aware and strictly increasing. Every return must be
finite and greater than `-1`. Candidate columns are sorted internally, and every
manifest candidate must have exactly one matching return column.

Each candidate manifest entry pins the exact SHA-256 of its backtest, robustness,
and statistical-validation JSON reports. Every report must be bound to the same
frozen dataset. Robustness and statistical reports must expose their deterministic
`report_digest` and `passed` state. Any missing report, digest mismatch, dataset
mismatch, omitted return column, or conflicting economic evidence fails closed.

When available, turnover is read from `metrics.turnover` in the backtest report.
The cost-to-gross-profit ratio is derived as:

```text
(commission_cost + slippage_cost)
-----------------------------------------------------------
(final_equity - initial_equity + commission_cost + slippage_cost)
```

The derived ratio is used only when gross profit is positive. Optional manifest
assertions must exactly match the report-derived values.

An example manifest is installed at:

```text
examples/portfolio_promotion_candidate_manifest.example.json
```

### Candidate promotion gates

A candidate is `eligible`, `rejected`, or `insufficient_evidence`. The policy can
require:

- passed robustness and statistical validation;
- minimum aligned OOS observations;
- positive or minimum cumulative net return;
- maximum drawdown within a configured limit;
- bounded turnover; and
- bounded cost-to-gross-profit ratio.

Rejected and insufficient candidates remain in the deterministic report with exact
reasons. They are never silently discarded.

### Diversification and allocation

The engine calculates Pearson, Spearman, and loss-period correlations. Candidates
that exceed either the ordinary or loss-period threshold are grouped into stable
redundancy clusters.

Two deterministic allocation methods are available:

- `equal_weight`
- `capped_inverse_volatility`

Inverse-volatility weights use only observations strictly before the rebalance
timestamp. Candidate caps, redundancy-cluster caps, rebalance frequency, and a
minimum cash reserve are enforced. Unallocatable capital remains cash rather than
violating a limit.

### Portfolio validation and stress tests

The base portfolio is evaluated in chronological folds, retaining each fold's
return, drawdown, pass/fail state, and reasons. The final promotion gate can require
a minimum walk-forward pass rate.

Every run also includes these deterministic scenarios:

- base allocation cost;
- adverse allocation cost;
- severe allocation cost;
- common-loss amplification; and
- removal of the largest average-weight candidate.

Reported portfolio metrics include cumulative and annualized return, annualized
volatility, Sharpe, Sortino, maximum drawdown, expected shortfall, turnover,
allocation cost, average cash weight, and effective strategy count.

### CLI

The installer adds the separate research-only command `wqs-portfolio` so the
existing default application and `wqs` CLI do not need risky edits.

```bash
wqs-portfolio \
  --candidate-manifest data/research/portfolio/candidates.json \
  --returns-csv data/research/portfolio/oos_returns.csv \
  --allocation capped_inverse_volatility \
  --minimum-candidate-observations 120 \
  --volatility-lookback 60 \
  --rebalance-frequency 20 \
  --minimum-cash-weight 0.05 \
  --maximum-candidate-weight 0.30 \
  --maximum-cluster-weight 0.45 \
  --adverse-cost-bps 10 \
  --severe-cost-bps 25 \
  --walk-forward-folds 4 \
  --minimum-fold-pass-rate 0.75 \
  --json-output data/research/portfolio/promotion-report.json
```

Possible final decisions are:

```text
promoted_for_shadow_research
rejected
insufficient_evidence
```

`promoted_for_shadow_research` is permission to continue offline or shadow
research only. It is not evidence of future profitability and is never live-trading
eligibility.
## Historical Shadow Portfolio Simulation v1

Historical Shadow Portfolio Simulation v1 replays promoted strategy sleeves in
strict timestamp order. It remains research-only and does not connect to a
network, broker, account, paper-trading endpoint, or order API.

The simulation enforces:

- point-in-time evidence visibility using `available_at`
- promoted, suspended, and retired candidate states
- evidence-age expiration
- decisions after the current observation and execution no earlier than a later
  observation
- equal-weight and capped inverse-volatility allocation using only returns
  observed at the decision timestamp
- candidate-weight, cash-reserve, turnover, and drawdown-halt limits
- transaction costs charged when a pending allocation executes
- deterministic decision journal, ledger, report UUID, and SHA-256 digest
- equal-weight, double-cost, extra-delay, and largest-candidate-removed scenarios
- fail-closed readiness decisions for the later forward-shadow research stage

Candidate returns use the same CSV layout as `wqs-portfolio`: `timestamp`
followed by at least two sorted or unsorted candidate columns. The parser sorts
candidate IDs deterministically and requires one finite return greater than
`-1` for every candidate and timestamp.

The point-in-time evidence manifest uses schema version 1 and pins the exact
`return_matrix_digest`; a different CSV fails closed. Each event contains a
candidate ID, strategy identity, `promoted`, `suspended`, or `retired` state,
`effective_at`, `available_at`, immutable evidence digest, and reason. A state is
never visible before `available_at`, even when its effective timestamp is
earlier. See `examples/historical_shadow_evidence_manifest.example.json`.

Run the offline CLI with:

```bash
wqs-shadow \
  --evidence-manifest data/research/historical-shadow-evidence.json \
  --returns-csv data/research/candidate-oos-returns.csv \
  --allocation capped_inverse_volatility \
  --initial-equity 1000000 \
  --rebalance-frequency 5 \
  --volatility-lookback 20 \
  --execution-delay-periods 1 \
  --minimum-active-candidates 2 \
  --minimum-cash-weight 0.05 \
  --maximum-candidate-weight 0.40 \
  --maximum-one-way-turnover 1 \
  --transaction-cost-bps 10 \
  --maximum-evidence-age-days 120 \
  --maximum-drawdown-before-halt -0.30 \
  --json-output reports/historical-shadow-v1.json
```

Run the deterministic synthetic validation with:

```bash
.venv/bin/python scripts/historical_shadow_simulation.py
```

A decision of `ready_for_forward_shadow_research` permits only the next offline
or no-order forward-shadow research stage. It is not permission for paper or
live trading and does not establish future profitability.
## Forward Shadow Mode v1

Forward Shadow Mode advances an append-only, crash-safe research checkpoint as
new strategy-return observations arrive. It is intentionally separated from
broker execution and consumes only externally supplied JSONL observation
batches plus the point-in-time evidence manifest produced by prior research
stages.

Safety posture:

- execution mode: `FORWARD_SHADOW`
- network access: disabled
- broker provider: none
- live trading: disabled
- order submission: disabled

The forward runner enforces:

- timezone-aware observation and receipt timestamps
- bounded future clock skew
- strictly increasing observation sequences and timestamps
- exact candidate-set agreement with the evidence manifest
- exactly-once observation identity and idempotent batch replay
- append-only evidence-manifest extension; prior evidence cannot be removed or
  modified
- point-in-time promotion, suspension, retirement, and evidence-age checks
- next-observation virtual allocation execution
- equal-weight and capped inverse-volatility allocation
- minimum cash and maximum candidate-weight controls
- one-way turnover limits and explicit simulated transaction costs
- stale-feed fail-closed suspension
- drawdown-triggered fail-closed suspension
- hash-chained decision journal and immutable observation IDs
- atomic checkpoint and batch-report persistence
- deterministic checkpoint identity independent of report creation time

Run one observation batch:

```bash
wqs-forward-shadow \
  --evidence-manifest data/research/shadow-evidence.json \
  --observations-jsonl data/forward-shadow/observations.jsonl \
  --state-file data/forward-shadow/state.json \
  --allocation capped_inverse_volatility \
  --rebalance-every-observations 5 \
  --execution-delay-observations 1 \
  --maximum-observation-lateness-seconds 900 \
  --maximum-drawdown-before-halt -0.30 \
  --json-output reports/forward-shadow-batch.json
```

An external scheduler may invoke this command after a read-only collector has
written a complete observation batch. This command does not poll a network,
read a brokerage account, create an order, or submit an order. A successful run
means only that research shadow state advanced consistently; it is not paper or
live trading approval and does not establish future profitability.

Run the deterministic synthetic restart and idempotency simulation with:

```bash
.venv/bin/python scripts/forward_shadow_simulation.py
```

## Broker Paper Execution & Reconciliation v1

This stage adds a broker-neutral safety foundation between Forward Shadow Mode
and any future external paper-broker transport.

It implements:

- offline certification of sandbox or paper account snapshots;
- strict account, endpoint, provider, environment, and currency fingerprints;
- long-only, integer-quantity, limit-order-only policy enforcement;
- deterministic intent IDs and deterministic `client_order_id` values;
- mandatory broker-side client-order-ID idempotency capability;
- stale market-data and stale account-snapshot rejection;
- order, notional, position, open-order, and cash-reserve limits;
- crash-safe SQLite order state and hash-chained order events;
- recovery of uncertain submissions by deterministic client order ID;
- explicit `NORMAL`, `SOFT_HALT`, `CANCEL_ONLY`, and `HARD_HALT` states;
- deterministic internal-versus-broker reconciliation;
- fail-closed halts for unknown orders, fills, positions, cash differences, or
  certified-environment mismatches;
- atomic certification, pre-trade, reconciliation, and inspection reports;
- a dedicated offline CLI named `wqs-paper`;
- a deterministic in-memory paper broker used only by simulation tests.

The CLI has no HTTP client, credentials, broker token, account query transport,
or order submission command. Actual external paper-broker connectivity remains
a separate certification stage.

Safety posture:

```text
Execution mode: PAPER_SAFETY_FOUNDATION
External broker transport: DISABLED
Supported environment declarations: SANDBOX or PAPER
Margin: DISABLED
Short selling: DISABLED
Fractional quantities: DISABLED
Live endpoint: UNREPRESENTABLE
Live trading: DISABLED
```

### Offline certification

```bash
wqs-paper certify \
  --account-snapshot examples/paper_account_snapshot.example.json \
  --policy examples/paper_execution_policy.example.json \
  --evaluated-at 2026-08-03T20:00:00Z \
  --require-order-submission \
  --json-output reports/paper-certification.json
```

### Offline pre-trade validation

```bash
wqs-paper validate-intent \
  --account-snapshot examples/paper_account_snapshot.example.json \
  --market-snapshot examples/paper_market_snapshot.example.json \
  --intent examples/paper_order_intent.example.json \
  --policy examples/paper_execution_policy.example.json \
  --store data/paper/paper-execution.sqlite3 \
  --evaluated-at 2026-08-03T20:00:00Z \
  --json-output reports/pretrade-decision.json
```

This command validates the order intent but never sends it anywhere.

### Offline reconciliation

```bash
wqs-paper reconcile \
  --internal-ledger examples/paper_internal_ledger.example.json \
  --account-snapshot examples/paper_account_snapshot.example.json \
  --policy examples/paper_execution_policy.example.json \
  --store data/paper/paper-execution.sqlite3 \
  --compared-at 2026-08-03T20:00:00Z \
  --json-output reports/paper-reconciliation.json
```

A discrepancy is recorded and activates the recommended kill switch. The
implementation never overwrites internal or broker values to hide a mismatch.

### Deterministic safety simulation

```bash
python scripts/paper_execution_simulation.py
```

The simulation verifies idempotent submission, timeout-after-acceptance
recovery, reconciliation, and kill-switch behavior using a networkless in-memory
paper broker. It is not evidence of investment profitability and is not approval
for external paper or live trading.

## Toss Read-Only Adapter Contract Certification v1

This stage certifies only the official Toss Securities Open API read-only
contract for account, holdings, and order-history evidence. The public
documentation identifies `https://openapi.tossinvest.com` as the API server and
does not document a separate paper or sandbox server, so this stage does not
pretend that a paper endpoint exists.

Safety boundaries:

- external network transport is absent and disabled by default
- credentials and access tokens are never loaded by this module
- captured response JSON is the only certification input
- only GET account, holdings, and order-history operations are allowlisted
- create, modify, cancel, conditional-order, and live-order paths are rejected
- `Authorization` values are redacted from all request evidence
- account selection is pinned by a SHA-256 fingerprint
- unknown order statuses, conflicting duplicates, missing request IDs, and
  repeated pagination tokens fail closed
- response-size, page-count, clock-skew, retry-budget, and rate-limit evidence
  are audited
- certification reports are deterministic and written atomically

Show the fixed contract:

```bash
.venv/bin/wqs-broker-certify show-contract
```

Run the synthetic, networkless capture certification:

```bash
.venv/bin/wqs-broker-certify certify-captures \
  --policy examples/toss_read_only_certification_policy.example.json \
  --captures examples/toss_read_only_capture_bundle.example.json \
  --certified-at 2026-08-03T21:30:00Z \
  --json-output reports/toss-read-only-certification.json
```

The example capture is synthetic and is not evidence from a real brokerage
account. A later stage may add an explicitly reviewed read-only transport, but
order submission, modification, and cancellation remain out of scope.

## Toss Order Write Dry-Run & Safety Certification v1

This phase compiles an internal paper-order intent into the pinned Toss order-create
contract without loading credentials or sending a request. It exists to validate the
write boundary before any external transport can be considered.

Pinned contract:

- OpenAPI version `1.2.9`
- base URL `https://openapi.tossinvest.com`
- `POST /api/v1/orders`
- OAuth bearer and `X-Tossinvest-Account` are required by the official contract
- `clientOrderId` is constrained to the official 36-character maximum

The v1 compiler is intentionally narrower than the broker contract:

- quantity-based orders only
- whole-share, long-only intents
- `LIMIT` orders only
- `DAY` time in force only
- no margin, leverage, shorting, amount orders, market orders, modify, or cancel
- orders below the pinned high-value-confirmation threshold only
- a current PASS read-only certification report is mandatory
- account, market, policy, and intent identities are digest-bound
- stale data, a non-normal kill switch, excess cash/position/notional risk, or a
  mismatched account fingerprint fails closed

A successful run produces a redacted compiled request, deterministic request digest,
and expiring human-approval challenge. It then passes the request to
`NoWriteDryRunTransport`, which always records:

```text
External network transport: DISABLED
Credentials loaded: NO
Broker write count: 0
Order submission: NOT SUBMITTED
Live trading: DISABLED
```

The package contains no network client dependency and exposes no order submission,
modification, replacement, or cancellation function. The compiled request uses only a
redacted bearer placeholder and a SHA-256 account fingerprint. It never persists an
access token or raw account sequence.

Show the pinned contract:

```bash
wqs-order-write-certify show-contract
```

Run the deterministic example certification:

```bash
wqs-order-write-certify certify-dry-run \
  --read-only-report examples/toss_read_only_certification_report.example.json \
  --intent examples/toss_order_write_intent.example.json \
  --account examples/toss_order_write_account.example.json \
  --market examples/toss_order_write_market.example.json \
  --policy examples/toss_order_write_policy.example.json \
  --evaluated-at 2026-08-03T21:31:00Z \
  --json-output reports/toss-order-write-dry-run.json
```

This phase is not broker paper trading and is not permission to place an order. The
next phase must isolate credentials and preserve the default-deny transport boundary;
actual write activation remains prohibited unless a separately certified environment
and explicit human-controlled release process exist.

## Credential Boundary & Token Isolation v1

This phase adds a networkless credential boundary that may read Toss credential
material from fixed environment-variable names, an injected macOS Keychain reader, or explicit test fixtures, but
never serializes raw client IDs, client secrets, account IDs, or token strings.

The implementation provides mutable loaded-secret buffers with best-effort explicit zeroization,
SHA-256-only credential descriptors, purpose-bound opaque token leases,
expiration and revocation checks, kill-switch enforcement, redacted
Authorization proofs, leak scanning, deterministic certification reports, and
atomic JSON output.

The token issuer used by certification is synthetic and performs no network
request. It exists only to test token lifecycle and secret handling. OAuth token
issuance, external HTTP transport, broker writes, order submission, order
modification, order cancellation, and live trading remain disabled.

Example commands:

```bash
wqs-credential-certify show-contract
wqs-credential-certify certify-fixture \
  --fixture examples/credential_isolation_fixture.example.json \
  --policy examples/credential_isolation_policy.example.json \
  --evaluated-at 2026-08-03T22:30:00Z \
  --json-output reports/credential-isolation.json
```

Environment certification reads only these fixed names:

- `WQS_TOSS_CLIENT_ID`
- `WQS_TOSS_CLIENT_SECRET`
- `WQS_TOSS_ACCOUNT_ID`
- `WQS_TOSS_CREDENTIAL_VERSION`

Do not place these values in Git, command-line arguments, JSON, SQLite, logs, or
reports.

The environment or Keychain remains owned by the caller; this phase zeroizes only the loaded boundary buffers and synthetic token buffers it creates.

## Disabled Write Transport Integration v1

This stage connects the order-write dry-run, human approval, credential lease,
and final broker transport boundary without enabling an external request.

Safety properties:

- the official Toss host and `POST /api/v1/orders` path are pinned
- approval is bound to one challenge, request digest, and account fingerprint
- the token lease must be active, purpose-bound, account-bound, and redacted
- the kill switch must remain `NORMAL`
- redirects and automatic order retries are disabled
- the only transport implementation returns `TRANSPORT_BLOCKED`
- `SUBMITTING`, `SUBMITTED`, `ACCEPTED`, and `FILLED` are not transport states
- network-call, redirect, and broker-write counts remain zero
- reports contain fingerprints and redacted headers, never raw credentials

Run the deterministic no-network integration simulation with:

```bash
.venv/bin/python scripts/disabled_write_transport_simulation.py
```

Inspect the contract with:

```bash
wqs-write-transport-certify show-contract
```

The `certify-fixture` command is an offline certification workflow using
explicitly fake credentials. It does not issue a real OAuth token and cannot
submit, modify, cancel, or retry an order.

```text
External network transport: DISABLED
Broker writes: DISABLED
Automatic order retries: DISABLED
Final state: TRANSPORT_BLOCKED
Live trading: DISABLED
```
