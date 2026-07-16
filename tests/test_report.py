"""
Tests for report.py HTML generation.

Verifies structure, content, escaping, and the plan tree flagging logic
without requiring a database or launching Flet.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.explain_parser import parse_explain_json
from core.skill_matcher import LedgerStatus, load_skills, match_skills
from report import generate_html_report, node_label

_SKILLS = load_skills().skills

_EXPLAIN_MISSING_INDEX = [{
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
}]

_EXPLAIN_CLEAN = [{
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
}]


def _make_result(explain_json):
    from cli import AnalysisResult
    plan = parse_explain_json(explain_json)
    diagnosis = match_skills(
        plan, _SKILLS,
        table_row_counts={"transactions": 500000},
        ledger_status=LedgerStatus.OK,
    )
    return AnalysisResult(diagnosis=diagnosis, plan=plan, schemas={})


# ---------------------------------------------------------------------------
# node_label() — shared pure helper
# ---------------------------------------------------------------------------

def test_node_label_with_relation_and_index():
    plan = parse_explain_json([{
        "Plan": {
            "Node Type": "Index Scan",
            "Relation Name": "orders",
            "Index Name": "idx_orders_id",
            "Plan Rows": 1,
            "Actual Rows": 1,
            "Total Cost": 4.0,
            "Actual Total Time": 0.1,
        },
        "Planning Time": 0.1,
        "Execution Time": 0.1,
    }])
    label = node_label(plan.root)
    assert "Index Scan" in label
    assert "orders" in label
    assert "idx_orders_id" in label
    assert "rows" in label


def test_node_label_without_relation():
    plan = parse_explain_json([{
        "Plan": {
            "Node Type": "Sort",
            "Plan Rows": 100,
            "Actual Rows": 100,
            "Total Cost": 10.0,
            "Actual Total Time": 1.0,
            "Plans": [],
        },
        "Planning Time": 0.1,
        "Execution Time": 1.1,
    }])
    label = node_label(plan.root)
    assert "Sort" in label
    assert "rows" in label


# ---------------------------------------------------------------------------
# generate_html_report() — structure
# ---------------------------------------------------------------------------

def test_report_is_valid_html_document():
    result = _make_result(_EXPLAIN_MISSING_INDEX)
    html = generate_html_report(result, "SELECT 1", host="db.bank.internal", dbname="core")
    assert html.strip().startswith("<!DOCTYPE html>")
    assert "<html" in html
    assert "</html>" in html
    assert "<head>" in html
    assert "<body>" in html


def test_report_header_contains_host_and_db_not_password():
    result = _make_result(_EXPLAIN_MISSING_INDEX)
    html = generate_html_report(
        result, "SELECT 1", host="db.bank.internal", dbname="coredb"
    )
    assert "db.bank.internal" in html
    assert "coredb" in html
    # Password must never appear — this is the key security assertion.
    # We don't pass password to generate_html_report() at all; verify it
    # has no parameter for it.
    import inspect
    params = inspect.signature(generate_html_report).parameters
    assert "password" not in params, "generate_html_report must not accept a password parameter"


def test_report_contains_query_text():
    result = _make_result(_EXPLAIN_MISSING_INDEX)
    query = "SELECT * FROM transactions WHERE account_id = 42"
    html = generate_html_report(result, query, host="localhost", dbname="db")
    assert query in html


def test_report_contains_finding_skill_name():
    result = _make_result(_EXPLAIN_MISSING_INDEX)
    html = generate_html_report(result, "SELECT 1", host="localhost", dbname="db")
    assert "missing_index" in html


def test_report_contains_full_explanation_not_just_description():
    result = _make_result(_EXPLAIN_MISSING_INDEX)
    html = generate_html_report(result, "SELECT 1", host="localhost", dbname="db")
    match = next(m for m in result.matches if m.skill_name == "missing_index")
    # explanation is the multi-paragraph text; description is the one-liner
    assert match.explanation.strip()[:40] in html
    assert "Suggested fix" in html
    assert match.fix_template.strip()[:40] in html


def test_report_summary_line_present_with_counts():
    result = _make_result(_EXPLAIN_MISSING_INDEX)
    html = generate_html_report(result, "SELECT 1", host="localhost", dbname="db")
    # Should contain "N finding(s)" summary
    assert "finding" in html.lower()


def test_report_clean_plan_shows_no_issues_message():
    result = _make_result(_EXPLAIN_CLEAN)
    html = generate_html_report(result, "SELECT 1", host="localhost", dbname="db")
    assert "No findings" in html or "No issues" in html


def test_report_plan_tree_section_present():
    result = _make_result(_EXPLAIN_MISSING_INDEX)
    html = generate_html_report(result, "SELECT 1", host="localhost", dbname="db")
    assert "Plan Tree" in html
    assert "Seq Scan" in html


def test_report_plan_tree_flags_matched_node_red():
    result = _make_result(_EXPLAIN_MISSING_INDEX)
    html = generate_html_report(result, "SELECT 1", host="localhost", dbname="db")
    # The flagged Seq Scan node should have the red color style applied.
    # Red color is #e53935 in our palette.
    assert "#e53935" in html
    # The warning prefix should appear for the flagged node.
    assert "⚠" in html


def test_report_clean_plan_tree_has_no_red_nodes():
    result = _make_result(_EXPLAIN_CLEAN)
    html = generate_html_report(result, "SELECT 1", host="localhost", dbname="db")
    # No skill fired, so no plan-tree node div should carry the red color.
    # The legend always contains #e53935 as a static color swatch, so we
    # check for the specific inline style that _render_html_node() emits on
    # flagged nodes (the color embedded in the monospace div style), not for
    # the color string in isolation.
    assert "color:#e53935;font-weight:bold" not in html


# ---------------------------------------------------------------------------
# HTML escaping — XSS guard
# ---------------------------------------------------------------------------

def test_query_text_is_html_escaped():
    result = _make_result(_EXPLAIN_CLEAN)
    malicious_query = 'SELECT 1; <script>alert("xss")</script>'
    html = generate_html_report(result, malicious_query, host="localhost", dbname="db")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_host_and_db_are_html_escaped():
    result = _make_result(_EXPLAIN_CLEAN)
    html = generate_html_report(
        result, "SELECT 1",
        host='host<b>"evil"</b>',
        dbname="db&<x>",
    )
    assert "<b>" not in html
    assert "&lt;b&gt;" in html
    assert "&amp;" in html
