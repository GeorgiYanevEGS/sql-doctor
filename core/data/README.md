# core/data/

This directory is a Python package resource location used by the PyInstaller
frozen binary to locate `coverage_ledger.json` at runtime.

## What lives here

- `__init__.py` — committed; makes this directory a proper Python package
- `coverage_ledger.json` — **NOT committed**; generated at build time

## How coverage_ledger.json gets here

`build.py` (project root) copies `tests/coverage_ledger.json` into this
directory immediately before running PyInstaller. The copy only happens if
the source ledger exists and is non-empty — run the CI `ledger-integrity`
job first to ensure the source is fresh.

In a source checkout `coverage_ledger.json` is intentionally absent here.
`skill_matcher.py` reads `tests/coverage_ledger.json` directly when
`sys.frozen` is False, so development and tests are unaffected.
