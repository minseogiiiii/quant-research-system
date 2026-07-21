#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

UV_BIN="$(command -v uv)"

"$UV_BIN" sync \
    --locked \
    --default-index https://pypi.org/simple

exec "$ROOT_DIR/.venv/bin/python" -m world_quant_system
