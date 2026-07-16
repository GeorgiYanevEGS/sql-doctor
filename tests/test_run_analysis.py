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
# LLM fallback wiring — run_analysis now invokes the grounded LLM path itself
# (previously only the CLI command did). Mock get_provider so no network/DB.
# ---------------------------------------------------------------------------

# A fully-cleared plan: an indexed lookup whose "Index Scan" node type is
# ledger-backed, so every node is SKILL_CLEARED (proven clean). The LLM fallback
# must NOT fire here — we don't second-guess a ledger-backed clean result.
_EXPLAIN_CLEAN = [
    {
        "Plan": {
            "Node Type": "Index Scan",
            "Relation Name": "transactions",
            "Index Name": "idx_transactions_account_id",
            "Plan Rows": 1,
            "Actual Rows": 1,
            "Total Cost": 4.0,
            "Actual Total Time": 0.05,
        },
        "Planning Time": 0.1,
        "Execution Time": 0.1,
    }
]

# A plan with genuine uncertainty: "Foreign Scan" is a real node type that no
# skill has a ledger entry for, so it comes back UNVERIFIED (covered by the "*"
# skill but not ledger-proven). This is the case the LLM fallback exists for.
_EXPLAIN_UNVERIFIED = [
    {
        "Plan": {
            "Node Type": "Foreign Scan",
            "Relation Name": "remote_ledger",
            "Plan Rows": 100,
            "Actual Rows": 100,
            "Total Cost": 200.0,
            "Actual Total Time": 50.0,
        },
        "Planning Time": 0.2,
        "Execution Time": 50.5,
    }
]


def _patch_db(explain_json):
    """Context managers patching the DB seams for run_analysis."""
    return (
        patch("cli._get_connection", return_value=_make_mock_conn(explain_json)),
        patch("cli.get_table_row_counts", return_value={"transactions": 500000}),
        patch("cli.introspect_query_tables", return_value={}),
    )


def test_run_analysis_llm_fires_on_unverified_node():
    from cli import run_analysis
    from core.llm_provider import LLMResponse

    fake = MagicMock()
    fake.is_available.return_value = True
    fake.complete.return_value = LLMResponse(
        text="Consider adding an index on account_id.",
        provider="ollama",
        model="qwen2.5-coder:7b",
    )
    db1, db2, db3 = _patch_db(_EXPLAIN_UNVERIFIED)
    with db1, db2, db3, patch("cli.get_provider", return_value=fake) as gp:
        result = run_analysis(
            dsn="postgresql://fake/db",
            query="SELECT 1",
            llm_provider="ollama",
            llm_model="qwen2.5-coder:7b",
            llm_host="http://localhost:11434",
        )

    assert not result.matches, "unverified plan should produce no deterministic matches"
    assert result.llm.attempted is True
    assert result.llm.response is not None
    assert "index" in result.llm.response.text.lower()
    # model + host must be threaded into the provider constructor.
    gp.assert_called_once()
    args, kwargs = gp.call_args
    assert args[0] == "ollama"
    assert kwargs.get("model") == "qwen2.5-coder:7b"
    assert kwargs.get("host") == "http://localhost:11434"


def test_run_analysis_llm_skipped_when_fully_skill_cleared():
    """
    A fully SKILL_CLEARED plan is a ledger-backed proven-clean result. Even with
    a provider selected, the LLM fallback must NOT fire — we don't second-guess
    a deterministically confirmed clean plan.
    """
    from cli import run_analysis

    db1, db2, db3 = _patch_db(_EXPLAIN_CLEAN)
    with db1, db2, db3, patch("cli.get_provider") as gp:
        result = run_analysis(
            dsn="postgresql://fake/db",
            query="SELECT 1",
            llm_provider="ollama",
            llm_model="qwen2.5-coder:7b",
        )

    assert not result.matches
    # Confirm the plan really is fully cleared (not just match-free).
    from core.skill_matcher import CoverageStatus
    assert all(
        s == CoverageStatus.SKILL_CLEARED
        for s in result.node_type_coverage.values()
    ), f"fixture must be fully SKILL_CLEARED, got {result.node_type_coverage}"
    assert result.llm.attempted is False
    # Skip reason must be explicit so the UI never shows a silent blank.
    assert result.llm.skipped_reason == "fully_cleared"
    gp.assert_not_called()


def test_run_analysis_llm_skipped_when_skill_matches():
    from cli import run_analysis

    db1, db2, db3 = _patch_db(_EXPLAIN_JSON)  # missing_index fires on this plan
    with db1, db2, db3, patch("cli.get_provider") as gp:
        result = run_analysis(
            dsn="postgresql://fake/db",
            query="SELECT 1",
            llm_provider="ollama",
        )

    assert result.matches, "missing_index should have fired"
    assert result.llm.attempted is False
    assert result.llm.skipped_reason == "deterministic_findings"
    gp.assert_not_called()


def test_run_analysis_llm_provider_none_never_attempts():
    from cli import run_analysis

    db1, db2, db3 = _patch_db(_EXPLAIN_CLEAN)
    with db1, db2, db3, patch("cli.get_provider") as gp:
        result = run_analysis(
            dsn="postgresql://fake/db",
            query="SELECT 1",
            llm_provider="none",
        )

    assert result.llm.attempted is False
    assert result.llm.skipped_reason == "no_provider"
    gp.assert_not_called()


def test_run_analysis_llm_unavailable_sets_error_without_raising():
    from cli import run_analysis

    fake = MagicMock()
    fake.is_available.return_value = False
    db1, db2, db3 = _patch_db(_EXPLAIN_UNVERIFIED)
    with db1, db2, db3, patch("cli.get_provider", return_value=fake):
        result = run_analysis(
            dsn="postgresql://fake/db",
            query="SELECT 1",
            llm_provider="ollama",
        )

    assert result.llm.attempted is True
    assert result.llm.error is not None
    assert result.llm.response is None
    fake.complete.assert_not_called()  # unavailable → never calls the model


def test_run_analysis_llm_call_failure_captured_as_error():
    from cli import run_analysis
    from core.llm_provider import LLMError

    fake = MagicMock()
    fake.is_available.return_value = True
    fake.complete.side_effect = LLMError("connection refused")
    db1, db2, db3 = _patch_db(_EXPLAIN_UNVERIFIED)
    with db1, db2, db3, patch("cli.get_provider", return_value=fake):
        result = run_analysis(
            dsn="postgresql://fake/db",
            query="SELECT 1",
            llm_provider="ollama",
        )

    assert result.llm.attempted is True
    assert result.llm.error is not None
    assert "connection refused" in result.llm.error


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
