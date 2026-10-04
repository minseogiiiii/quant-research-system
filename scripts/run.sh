#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

unset PYTHONHOME PYTHONPATH

unset UV_NO_INSTALL_LOCAL UV_NO_INSTALL_PROJECT UV_NO_INSTALL_WORKSPACE
unset UV_NO_PROJECT UV_NO_SYNC UV_NO_EDITABLE UV_PROJECT_ENVIRONMENT
unset UV_INDEX UV_INDEX_URL UV_EXTRA_INDEX_URL UV_DEFAULT_INDEX

uv sync \
    --locked \
    --no-editable \
    --reinstall-package world-quant-system \
    --default-index https://pypi.org/simple

exec "$ROOT_DIR/.venv/bin/python" -m world_quant_system
