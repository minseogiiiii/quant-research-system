# World Quant System

Broker-neutral foundation for a quantitative trading system designed around three priorities:

1. Research efficiency
2. Risk-controlled execution
3. Broker independence

The current project is intentionally limited to a safe mock environment. It contains no live-order API and no real brokerage credentials.

## Current modes

- `mock`: deterministic in-memory test data
- `replay`: reserved for historical-data playback
- `shadow`: reserved for read-only broker data
- `live`: intentionally blocked

## Setup

```bash
uv python install 3.12
uv sync
```

Do not mix `uv pip install -e .` with the project workflow. Use `uv sync` and `uv run` so the lockfile and environment remain consistent.

## Run

```bash
uv run python -m world_quant_system
uv run world-quant-system
```

Both commands must print `Execution mode: MOCK` and `Live trading: DISABLED`.

## Validate everything

```bash
./scripts/check.sh
```

The check script performs a locked environment sync, unit tests, package-entrypoint smoke tests, Ruff, mypy, and both application entrypoints.

## Security rules

- Never commit `.env.local` or any brokerage secret.
- Never give an LLM access to live brokerage credentials.
- Do not add an order endpoint before a deterministic risk gate and reconciliation system exist.
- Keep broker-specific code behind broker-neutral interfaces.
