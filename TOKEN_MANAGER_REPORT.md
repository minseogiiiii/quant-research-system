# Toss Token Manager Validation Report

## Scope

This release adds a networkless OAuth token lifecycle foundation. It does not
contain a client ID, client secret, token endpoint implementation, account
access, market-data request, or order capability.

The design follows the official Toss Securities Open API 1.2.4 contract:

- OAuth 2.0 Client Credentials Grant
- `access_token`, `token_type`, and `expires_in` response fields
- no refresh token
- token reissuance through the same endpoint after expiration
- one valid access token per client; reissuance immediately invalidates the
  previous token

## Added code

- `token.py`
  - masked `TokenIssueResponse`
  - masked `AccessToken`
  - non-sensitive `TokenMetadata`
- `token_manager.py`
  - dependency-injected `TokenIssuer`
  - fail-closed `NoNetworkTokenIssuer`
  - single-flight concurrent issuance
  - monotonic expiration tracking
  - configurable early-refresh window
  - explicit invalidation and forced refresh
  - fail-closed handling for ambiguous issuance failures

## Safety properties checked

- Default manager cannot perform a network request.
- Token values do not appear in `repr` or `str`.
- One hundred concurrent callers share one successful issuance.
- One hundred concurrent callers share one failed issuance.
- Cancelling one waiter does not cancel issuance for other waiters.
- Invalid token shape, token type, and expiration are rejected.
- Known adapter errors are preserved.
- Unexpected issuer failures are safely wrapped.
- Failed refresh does not return a possibly invalidated old token.
- Wall-clock metadata is timezone-aware; freshness uses a monotonic clock.
- Short-lived tokens cannot be consumed entirely by the configured skew.

## Deterministic simulation

A seven-day state-machine simulation calls the manager every five minutes with
an 86,400-second token lifetime and five-minute refresh skew. It verifies that
no expired token is served and that exactly eight token issuances occur.

This is a software token-lifecycle simulation, not a trading-strategy backtest.
There is no market data or strategy in this release, so it provides no evidence
of investment performance or profitability.

## Validation results

Executed in the available isolated environment with Python 3.13.5. The project
continues to target Python 3.12 and 3.13.

- Python compileall: passed
- pytest: 72 passed
- Ruff: passed
- mypy strict: passed for 28 source/test files
- module entry point: passed
- wheel build: passed
- wheel install and outside-repository import: passed
- wheel module/console output equality: passed
- default HTTP transport network block: passed
- default token issuer network block: passed

The environment had no outbound DNS access, so `uv sync` could not download a
separate Python 3.12 runtime. No new dependency was added and `uv.lock` was not
changed.
