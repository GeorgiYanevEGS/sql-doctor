"""
Tests for the run_analysis() callable extracted from cli.py's analyze command.

Verifies:
- status callbacks fire in the expected sequence
- SkillMatch results are returned (not printed)
- CLI analyze command behaviour is unchanged (tested via subprocess to catch
  any regression in the typer wiring)
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# run_analysis unit tests (no DB required — mock psycopg2 + EXPLAIN output)
# ---------------------------------------------------------------------------

_EXPLAIN_JSON = [
    {
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "transactions",
            "Filter": "(account_id = 42)",
            "Plan Rows": 1,
            "Actual Rows": 15000,
            "Total Cost": 45000.0,
            "Actual Total Time": 320.5,
        },
        "Planning Time": 0.3,
        "Execution Time": 321.0,
    }
]


def _make_mock_conn(explain_json=None):
    """Return a mock psycopg2 connection whose cursor fetchone returns explain_json."""
    if explain_json is None:
        explain_json = _EXPLAIN_JSON
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = lambda s: s
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchone.return_value = [explain_json]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    return mock_conn


def test_run_analysis_returns_diagnosis():
    from cli import run_analysis

    with patch("cli._get_connection", return_value=_make_mock_conn()), \
         patch("cli.get_table_row_counts", return_value={"transactions": 500000}), \
         patch("cli.introspect_query_tables", return_value={}):
        result = run_analysis(
            dsn="postgresql://fake/db",
            query="SELECT 1",
            llm_provider="none",
            schema="public",
        )

    names = {m.skill_name for m in result.matches}
    assert "missing_index" in names


def test_run_analysis_status_callbacks_fire_in_order():
    from cli import run_analysis

    statuses = []

    with patch("cli._get_connection", return_value=_make_mock_conn()), \
         patch("cli.get_table_row_counts", return_value={"transactions": 500000}), \
         patch("cli.introspect_query_tables", return_value={}):
        run_analysis(
            dsn="postgresql://fake/db",
            query="SELECT 1",
            llm_provider="none",
            schema="public",
            on_status=statuses.append,
        )

    # Must include at least the EXPLAIN stage and the skill-check stage.
    joined = " ".join(statuses)
    assert "EXPLAIN" in joined, f"expected EXPLAIN status, got {statuses}"
    assert any("skill" in s.lower() or "deterministic" in s.lower() for s in statuses), (
        f"expected skill-check status, got {statuses}"
    )
    # Callbacks must fire in order — EXPLAIN before skills.
    explain_idx = next(i for i, s in enumerate(statuses) if "EXPLAIN" in s)
    skill_idx = next(
        i for i, s in enumerate(statuses)
        if "skill" in s.lower() or "deterministic" in s.lower()
    )
    assert explain_idx < skill_idx, "EXPLAIN status must come before skill-check status"


def test_run_analysis_no_on_status_does_not_raise():
    from cli import run_analysis

    with patch("cli._get_connection", return_value=_make_mock_conn()), \
         patch("cli.get_table_row_counts", return_value={"transactions": 500000}), \
         patch("cli.introspect_query_tables", return_value={}):
        # Must not raise even when on_status is None (the default).
        run_analysis(
            dsn="postgresql://fake/db",
            query="SELECT 1",
            llm_provider="none",
            schema="public",
        )


def test_run_analysis_description_populated():
    from cli import run_analysis

    with patch("cli._get_connection", return_value=_make_mock_conn()), \
         patch("cli.get_table_row_counts", return_value={"transactions": 500000}), \
         patch("cli.introspect_query_tables", return_value={}):
        result = run_analysis(
            dsn="postgresql://fake/db",
            query="SELECT 1",
            llm_provider="none",
            schema="public",
        )

    match = next(m for m in result.matches if m.skill_name == "missing_index")
    assert match.description.strip(), "description must be non-empty on returned SkillMatch"


# ---------------------------------------------------------------------------
# CLI smoke test — verify analyze command still works via subprocess
# ---------------------------------------------------------------------------

def test_cli_analyze_still_exits_cleanly(tmp_path):
    """
    The analyze typer command must still work from the command line after
    the run_analysis extraction. Uses --help to avoid needing a real DB.
    """
    result = subprocess.run(
        [sys.executable, "cli.py", "analyze", "--help"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert result.returncode == 0, f"cli.py analyze --help exited {result.returncode}:\n{result.stderr}"
    assert "--dsn" in result.stdout
    assert "--query" in result.stdout
