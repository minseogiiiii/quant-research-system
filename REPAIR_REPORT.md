# Repair Report

## Root causes found

1. The package build configuration changed repeatedly between `uv_build` and
   Hatchling while the same `.venv` was reused.
2. The environment sometimes contained package metadata without a working
   editable source-path link, so `uv pip show` succeeded while
   `import world_quant_system` failed.
3. `pytest` temporarily used `pythonpath = ["src"]`, which could hide a broken
   package installation.
4. `config/__init__.py` still contained obsolete KIS configuration after the
   project migrated to broker-neutral Toss settings.
5. Toss adapter package exports and error tests were incomplete.
6. Shell here-document commands were accidentally pasted into Python editor
   buffers during manual repairs.

## Repairs applied

- Standardized packaging on Setuptools with an explicit `src` package layout.
- Removed pytest `pythonpath` injection.
- Made the check script force a clean local package reinstall and verify imports
  from outside the repository.
- Added explicit protection against uv environment variables that skip local
  project installation or change package indexes.
- Replaced obsolete KIS exports with broker-neutral settings exports.
- Completed Toss adapter exports, error tests, schema tests, response mapping
  tests, and fail-closed transport tests.
- Removed unused empty integration/data placeholders and the unused composite
  `Broker` compatibility module.
- Added a one-command environment reset script.

## Validation performed

- Source/test bytecode compilation
- Editable package installation with source-path verification
- 48 pytest tests
- Ruff
- mypy strict mode
- Wheel build and wheel content inspection
- Installed-wheel import from outside the repository
- Module and console entry points
- Default Toss transport network-blocking behavior
- Public-PyPI lockfile URL check

The sandbox runtime provided Python 3.13.5. Static tooling is configured for
Python 3.12, and the project declares support for Python 3.12 and 3.13. A fresh
Python 3.12 runtime test must run on the user's Mac through `scripts/check.sh`.
