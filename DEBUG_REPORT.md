# Debug and cleanup report

## Result

The uploaded project could not reproduce the repeated `ModuleNotFoundError` in a clean environment: after a fresh sync, both Python module execution and the installed console script worked. This indicates that the recurring failure was primarily caused by the local virtual-environment/install state rather than the application logic.

The original test configuration contained `pythonpath = ["src"]`. That setting allowed tests to import source files even when the project package or console entry point was not correctly installed, so the tests could pass while the actual application failed. The cleaned project removes that masking behavior and adds smoke tests for both real entry points.

## Changes made

- Replaced the specialized `uv_build` configuration with Hatchling and an explicit `src/world_quant_system` package path.
- Removed pytest's manual `src` path injection.
- Added smoke tests for:
  - `python -m world_quant_system`
  - the installed `world-quant-system` console script
- Made `world_quant_system/__init__.py` side-effect free.
- Kept `__main__.py` and the console entry point pointed directly at `world_quant_system.app:main`.
- Split the old generic broker base into read-only interfaces:
  - `MarketDataProvider`
  - `PortfolioReader`
- Kept `MockBroker` read-only and made its internal positions immutable.
- Strengthened safe configuration rules:
  - live mode is blocked
  - live-trading flag is blocked
  - mock/replay cannot connect to a broker
  - shadow mode requires the Toss provider
- Removed unused Toss credentials until the Toss authentication phase is actually implemented.
- Removed currently unused runtime dependencies (`httpx`, `structlog`, `tenacity`). They should be added back only when the Toss adapter uses them.
- Removed empty future package directories. Add them back when their implementations begin.
- Added model-validation tests and package-entrypoint smoke tests.
- Added a single `scripts/check.sh` command for the complete validation sequence.
- Added a complete `.gitignore` and expanded the README.
- Removed Python caches and generated files from the deliverable.

## Verification performed

The cleaned project was checked through three independent installation paths:

1. Fresh editable project environment
2. Forced editable project reinstall
3. Built wheel installed into a separate isolated virtual environment

Across those paths:

- 20 tests passed
- Ruff passed
- mypy strict mode passed
- module entry point passed
- console-script entry point passed
- module and console outputs matched exactly
- the wheel contained all required package modules

The sandbox had Python 3.13 available, so runtime verification was performed on Python 3.13. The project supports Python 3.12 and 3.13, and its static-analysis target remains Python 3.12.

## Clean installation on the Mac

From the cleaned project directory:

```bash
rm -rf .venv
uv sync --locked
./scripts/check.sh
```

Do not use `uv pip install -e .` for this project workflow. Do not manually delete `.venv/bin/world-quant-system`. Let `uv sync` own the environment and installed entry points.
