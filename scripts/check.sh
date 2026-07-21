#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

unset PYTHONHOME PYTHONPATH

UV_BIN="$(command -v uv || true)"
if [[ -z "$UV_BIN" ]]; then
    echo "ERROR: uv was not found in PATH."
    exit 1
fi

if [[ ! -f uv.lock ]]; then
    echo "ERROR: uv.lock is missing. Run: uv lock"
    exit 1
fi

if grep -qE 'applied-caas|internal\.api\.openai\.org' uv.lock; then
    echo "ERROR: uv.lock contains a private package index."
    exit 1
fi

# Prevent user-level uv settings from silently skipping the local project.
unset UV_NO_INSTALL_LOCAL UV_NO_INSTALL_PROJECT UV_NO_INSTALL_WORKSPACE
unset UV_NO_PROJECT UV_NO_SYNC UV_NO_EDITABLE UV_PROJECT_ENVIRONMENT
unset UV_INDEX UV_INDEX_URL UV_EXTRA_INDEX_URL UV_DEFAULT_INDEX

"$UV_BIN" sync \
    --locked \
    --no-editable \
    --reinstall-package world-quant-system \
    --default-index https://pypi.org/simple

PYTHON="$ROOT_DIR/.venv/bin/python"
RUFF="$ROOT_DIR/.venv/bin/ruff"
MYPY="$ROOT_DIR/.venv/bin/mypy"
CONSOLE="$ROOT_DIR/.venv/bin/world-quant-system"

for executable in "$PYTHON" "$RUFF" "$MYPY" "$CONSOLE"; do
    if [[ ! -x "$executable" ]]; then
        echo "ERROR: Required executable is missing: $executable"
        exit 1
    fi
done

echo "1/8 Verifying the installed package outside the repository..."
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
(
    cd "$TMP_DIR"
    env -u PYTHONPATH PYTHONNOUSERSITE=1 "$PYTHON" -c '
import world_quant_system
from world_quant_system.adapters.toss import (
    HttpMethod,
    TossAdapterError,
    TossHttpClient,
    TossMarketDataParser,
    TossMarketDataProvider,
    TokenIssueResponse,
    TossRequest,
    TossResponse,
    TossTokenManager,
)
from world_quant_system.domain import Candle, CandleInterval, CandlePage

print(f"Package: {world_quant_system.__file__}")
print(f"Client: {TossHttpClient.__name__}")
print(f"Token manager: {TossTokenManager.__name__}")
print(f"Market data: {TossMarketDataProvider.__name__}, {TossMarketDataParser.__name__}")
print(f"Domain: {Candle.__name__}, {CandleInterval.__name__}, {CandlePage.__name__}")
print(f"Token response: {TokenIssueResponse.__name__}")
print(f"Error: {TossAdapterError.__name__}")
print(f"Schemas: {HttpMethod.__name__}, {TossRequest.__name__}, {TossResponse.__name__}")
'
)

echo "2/8 Compiling source and tests..."
"$PYTHON" -m compileall -q src tests

echo "3/8 Running tests..."
"$PYTHON" -m pytest -q

echo "4/8 Running Ruff..."
"$RUFF" check .

echo "5/8 Running mypy..."
"$MYPY" src tests

echo "6/8 Verifying fail-closed network behavior..."
"$PYTHON" -c '
import asyncio
from world_quant_system.adapters.toss import (
    TossHttpClient,
    TossMarketDataProvider,
    TossTokenManager,
    TossTransportError,
)

async def verify() -> None:
    client = TossHttpClient("https://example.test")
    try:
        await client.get("/prices")
    except TossTransportError as error:
        assert str(error) == "Network transport is not configured."
    else:
        raise AssertionError("Default transport unexpectedly allowed a request.")

    provider = TossMarketDataProvider(client)
    try:
        await provider.get_quote("005930")
    except TossTransportError as error:
        assert str(error) == "Network transport is not configured."
    else:
        raise AssertionError("Market-data provider unexpectedly allowed a request.")

    manager = TossTokenManager()
    try:
        await manager.get_token()
    except TossTransportError as error:
        assert str(error) == "Token issuer is not configured."
    else:
        raise AssertionError("Default token issuer unexpectedly issued a token.")

asyncio.run(verify())
'

echo "7/8 Testing module entry point..."
MODULE_OUTPUT="$(
    cd "$TMP_DIR"
    env -u PYTHONPATH PYTHONNOUSERSITE=1 \
        "$PYTHON" -m world_quant_system
)"

echo "8/8 Testing console entry point..."
CONSOLE_OUTPUT="$(
    cd "$TMP_DIR"
    env -u PYTHONPATH PYTHONNOUSERSITE=1 "$CONSOLE"
)"

if [[ "$MODULE_OUTPUT" != "$CONSOLE_OUTPUT" ]]; then
    echo "ERROR: Module and console outputs differ."
    diff \
        <(printf '%s\n' "$MODULE_OUTPUT") \
        <(printf '%s\n' "$CONSOLE_OUTPUT") || true
    exit 1
fi

printf '%s\n' "$MODULE_OUTPUT"
echo
echo "All project checks passed."
