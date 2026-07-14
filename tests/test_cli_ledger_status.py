"""
Tests for the `sql-doctor ledger-status` CLI command.

The command is the machine-parseable interface for CI smoke checks — it
reads the coverage ledger and reports LEDGER_STATUS=OK/MISSING/CORRUPT
on stdout with a matching exit code (0/1/2). No database, no query, no
provider — just "did the bundled ledger load."
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from typer.testing import CliRunner

from cli import app

runner = CliRunner()


def test_ledger_status_ok_with_real_ledger():
    """Default invocation against the committed ledger must exit 0 and print LEDGER_STATUS=OK."""
    result = runner.invoke(app, ["ledger-status"])
    assert result.exit_code == 0, f"expected exit 0, got {result.exit_code}\n{result.output}"
    assert "LEDGER_STATUS=OK" in result.output


def test_ledger_status_missing_when_ledger_absent(tmp_path):
    """Pointing at a non-existent path must exit 1 and print LEDGER_STATUS=MISSING."""
    result = runner.invoke(app, ["ledger-status", "--ledger-path", str(tmp_path / "nope.json")])
    assert result.exit_code == 1, f"expected exit 1, got {result.exit_code}\n{result.output}"
    assert "LEDGER_STATUS=MISSING" in result.output


def test_ledger_status_corrupt_when_ledger_invalid(tmp_path):
    """Pointing at a file with invalid JSON must exit 2 and print LEDGER_STATUS=CORRUPT."""
    bad = tmp_path / "bad.json"
    bad.write_text("not json at all", encoding="utf-8")
    result = runner.invoke(app, ["ledger-status", "--ledger-path", str(bad)])
    assert result.exit_code == 2, f"expected exit 2, got {result.exit_code}\n{result.output}"
    assert "LEDGER_STATUS=CORRUPT" in result.output
