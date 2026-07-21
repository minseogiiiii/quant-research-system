#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

UV_BIN="$(command -v uv)"

if [[ -z "$UV_BIN" ]]; then
    echo "ERROR: uv was not found."
    exit 1
fi

if [[ ! -f "uv.lock" ]]; then
    echo "ERROR: uv.lock is missing."
    exit 1
fi

if grep -qE 'applied-caas|internal\.api\.openai\.org' uv.lock; then
    echo "ERROR: uv.lock contains a private package index."
    exit 1
fi

echo "1/7 Installing project in non-editable mode..."
"$UV_BIN" sync \
    --locked \
    --no-editable \
    --default-index https://pypi.org/simple

PYTHON="$ROOT_DIR/.venv/bin/python"
CONSOLE="$ROOT_DIR/.venv/bin/world-quant-system"

if [[ ! -x "$PYTHON" ]]; then
    echo "ERROR: Project Python was not created."
    exit 1
fi

if [[ ! -x "$CONSOLE" ]]; then
    echo "ERROR: Console entry point was not installed."
    exit 1
fi

echo "2/7 Verifying installed package..."
"$PYTHON" -c "
import world_quant_system
print(world_quant_system.__file__)
"

echo "3/7 Running tests..."
"$PYTHON" -m pytest -q

echo "4/7 Running Ruff..."
"$ROOT_DIR/.venv/bin/ruff" check .

echo "5/7 Running mypy..."
"$ROOT_DIR/.venv/bin/mypy" src tests

echo "6/7 Testing module entry point..."
MODULE_OUTPUT="$("$PYTHON" -m world_quant_system)"

echo "7/7 Testing console entry point..."
CONSOLE_OUTPUT="$("$CONSOLE")"

if [[ "$MODULE_OUTPUT" != "$CONSOLE_OUTPUT" ]]; then
    echo "ERROR: Module and console outputs differ."
    diff <(printf '%s\n' "$MODULE_OUTPUT") \
         <(printf '%s\n' "$CONSOLE_OUTPUT") || true
    exit 1
fi

printf '%s\n' "$MODULE_OUTPUT"
echo
echo "All project checks passed using the installed package."
