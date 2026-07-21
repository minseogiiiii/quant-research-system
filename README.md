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

The check script performs a locked sync, forces the local project package to be
reinstalled, verifies imports from outside the repository, compiles source and
tests, runs pytest, Ruff, mypy, validates the network kill switch, and checks
both application entry points.

## Current Toss adapter scope

The Toss adapter currently provides only:

- immutable request and response models
- typed, sanitized exception classes
- an injected transport protocol
- a default `NoNetworkTransport` that always refuses requests
- deterministic HTTP status-to-error mapping

There is no real HTTP implementation, OAuth flow, account access, or order
execution.

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
