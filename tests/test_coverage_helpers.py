"""
Tests for the coverage ledger helper — assert_no_match and VacuousTestError.
These tests use a temporary ledger path to avoid polluting the real ledger.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.skill_matcher import CoverageStatus, load_skills, match_skills
from core.explain_parser import parse_explain_json
from tests.coverage_helpers import VacuousTestError, assert_no_match

SKILLS = load_skills()


def test_assert_no_match_raises_on_vacuous_plan(tmp_path):
    """
    assert_no_match(..., 'Seq Scan', ...) against a plan that contains only
    a Hash Join must raise VacuousTestError — not silently pass and write a
    misleading ledger entry.
    """
    hash_join_only_plan = [
        {
            "Plan": {
                "Node Type": "Hash Join",
                "Relation Name": None,
                "Plan Rows": 100,
                "Actual Rows": 100,
                "Total Cost": 100.0,
                "Actual Total Time": 5.0,
            },
            "Planning Time": 0.1,
            "Execution Time": 5.1,
        }
    ]
    raised = False
    try:
        assert_no_match("missing_index", "Seq Scan", hash_join_only_plan, SKILLS, ledger_path=tmp_path / "ledger.json")
    except VacuousTestError:
        raised = True
    assert raised, "Expected VacuousTestError when plan contains no Seq Scan node"


def test_assert_no_match_writes_ledger_entry_on_success(tmp_path):
    """
    A Seq Scan at high selectivity: missing_index doesn't fire, so
    assert_no_match should succeed and write (missing_index, Seq Scan)
    to the ledger.
    """
    ledger_path = tmp_path / "ledger.json"
    high_selectivity_seq_scan = [
        {
            "Plan": {
                "Node Type": "Seq Scan",
                "Relation Name": "transactions",
                "Filter": "((txn_type)::text = 'OPER'::text)",
                "Plan Rows": 120000,
                "Actual Rows": 119884,
                "Total Cost": 4606.0,
                "Actual Total Time": 27.0,
            },
            "Planning Time": 1.0,
            "Execution Time": 28.0,
        }
    ]
    assert_no_match(
        "missing_index",
        "Seq Scan",
        high_selectivity_seq_scan,
        SKILLS,
        table_row_counts={"transactions": 200000},
        ledger_path=ledger_path,
    )
    entries = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert {"skill_name": "missing_index", "node_type": "Seq Scan"} in entries


def test_unverified_coverage_does_not_produce_skill_cleared(tmp_path):
    """
    When load_skills is given a ledger that has NO entry for
    (missing_index, Seq Scan), a Seq Scan that doesn't trigger missing_index
    must produce UNVERIFIED — not SKILL_CLEARED — because the negative test
    contract hasn't been fulfilled for that (skill, node_type) pair.
    """
    # Empty ledger — no negative tests recorded for anything
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text("[]", encoding="utf-8")

    skills = load_skills(ledger_path=ledger_path)

    high_selectivity_seq_scan = [
        {
            "Plan": {
                "Node Type": "Seq Scan",
                "Relation Name": "transactions",
                "Filter": "((txn_type)::text = 'OPER'::text)",
                "Plan Rows": 120000,
                "Actual Rows": 119884,
                "Total Cost": 4606.0,
                "Actual Total Time": 27.0,
            },
            "Planning Time": 1.0,
            "Execution Time": 28.0,
        }
    ]
    plan = parse_explain_json(high_selectivity_seq_scan)
    result = match_skills(plan, skills, table_row_counts={"transactions": 200000})
    assert not result.matches
    assert result.node_type_coverage.get("Seq Scan") == CoverageStatus.UNVERIFIED, (
        f"expected UNVERIFIED with empty ledger, got {result.node_type_coverage.get('Seq Scan')}"
    )


def test_verified_coverage_produces_skill_cleared(tmp_path):
    """
    When the ledger contains (missing_index, Seq Scan), a Seq Scan that
    doesn't trigger missing_index must produce SKILL_CLEARED.
    """
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(
        '[{"skill_name": "missing_index", "node_type": "Seq Scan"}]',
        encoding="utf-8",
    )

    skills = load_skills(ledger_path=ledger_path)

    high_selectivity_seq_scan = [
        {
            "Plan": {
                "Node Type": "Seq Scan",
                "Relation Name": "transactions",
                "Filter": "((txn_type)::text = 'OPER'::text)",
                "Plan Rows": 120000,
                "Actual Rows": 119884,
                "Total Cost": 4606.0,
                "Actual Total Time": 27.0,
            },
            "Planning Time": 1.0,
            "Execution Time": 28.0,
        }
    ]
    plan = parse_explain_json(high_selectivity_seq_scan)
    result = match_skills(plan, skills, table_row_counts={"transactions": 200000})
    assert not result.matches
    assert result.node_type_coverage.get("Seq Scan") == CoverageStatus.SKILL_CLEARED, (
        f"expected SKILL_CLEARED with ledger entry present, got {result.node_type_coverage.get('Seq Scan')}"
    )


def test_assert_no_match_fails_when_skill_fires(tmp_path):
    """
    assert_no_match must raise AssertionError (not write a ledger entry)
    when the skill actually fires on a node of the claimed type.
    """
    ledger_path = tmp_path / "ledger.json"
    low_selectivity_seq_scan = [
        {
            "Plan": {
                "Node Type": "Seq Scan",
                "Relation Name": "transactions",
                "Filter": "(account_id = 42)",
                "Plan Rows": 1,
                "Actual Rows": 12000,
                "Total Cost": 45000.0,
                "Actual Total Time": 320.0,
            },
            "Planning Time": 0.3,
            "Execution Time": 321.0,
        }
    ]
    raised = False
    try:
        assert_no_match(
            "missing_index",
            "Seq Scan",
            low_selectivity_seq_scan,
            SKILLS,
            table_row_counts={"transactions": 500000},
            ledger_path=ledger_path,
        )
    except AssertionError:
        raised = True
    assert raised, "Expected AssertionError when skill fires on a claimed node type"
    assert not ledger_path.exists(), "Ledger must not be written when the skill fires"
