"""
Pytest configuration for the sql-doctor test suite.

Serial guard: tests marked @pytest.mark.serial write to coverage_ledger.json
as a side effect. Concurrent writes race — two processes can both read the
file, both compute a new state, and the later write silently clobbers the
earlier one. This hook turns that silent corruption into a loud failure if
someone ever runs the suite under pytest-xdist.
"""

import os

import pytest


def pytest_runtest_setup(item):
    if item.get_closest_marker("serial") and os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.fail(
            f"Test '{item.nodeid}' is marked @pytest.mark.serial and must not run "
            f"under a pytest-xdist worker ('{os.environ['PYTEST_XDIST_WORKER']}'). "
            "Ledger-writing tests require serial execution to avoid concurrent file writes. "
            "Run pytest without -n to execute these tests."
        )
