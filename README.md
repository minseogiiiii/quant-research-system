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
- `replay`: reserved for historical-data playback
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
