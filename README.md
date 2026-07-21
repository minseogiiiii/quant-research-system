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
