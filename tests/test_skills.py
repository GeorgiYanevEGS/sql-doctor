"""
Sanity tests that don't require a real database — they feed synthetic
EXPLAIN JSON straight into the parser and skill matcher, simulating what
psycopg2 would return for three classic anti-patterns.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.explain_parser import parse_explain_json
from core.skill_matcher import load_skills, match_skills

SKILLS = load_skills()


def test_missing_index():
    explain_json = [
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
    plan = parse_explain_json(explain_json)
    matches = match_skills(plan, SKILLS, table_row_counts={"transactions": 500000})
    names = {m.skill_name for m in matches}
    assert "missing_index" in names, f"expected missing_index, got {names}"
    print("PASS: test_missing_index ->", [m.skill_name for m in matches])


def test_implicit_conversion():
    explain_json = [
        {
            "Plan": {
                "Node Type": "Seq Scan",
                "Relation Name": "customers",
                "Filter": "(upper((email)::text) = 'TEST@BANK.COM'::text)",
                "Plan Rows": 1,
                "Actual Rows": 1,
                "Total Cost": 12000.0,
                "Actual Total Time": 88.0,
            },
            "Planning Time": 0.2,
            "Execution Time": 88.3,
        }
    ]
    plan = parse_explain_json(explain_json)
    matches = match_skills(plan, SKILLS)
    names = {m.skill_name for m in matches}
    assert "implicit_type_conversion" in names, f"expected implicit_type_conversion, got {names}"
    print("PASS: test_implicit_conversion ->", [m.skill_name for m in matches])


def test_stale_statistics():
    explain_json = [
        {
            "Plan": {
                "Node Type": "Nested Loop",
                "Relation Name": None,
                "Plan Rows": 5,
                "Actual Rows": 250000,
                "Total Cost": 900.0,
                "Actual Total Time": 4200.0,
                "Plans": [
                    {
                        "Node Type": "Index Scan",
                        "Relation Name": "accounts",
                        "Index Name": "accounts_pkey",
                        "Plan Rows": 5,
                        "Actual Rows": 250000,
                        "Total Cost": 400.0,
                        "Actual Total Time": 2000.0,
                    }
                ],
            },
            "Planning Time": 0.5,
            "Execution Time": 4201.0,
        }
    ]
    plan = parse_explain_json(explain_json)
    matches = match_skills(plan, SKILLS)
    names = {m.skill_name for m in matches}
    assert "stale_statistics" in names, f"expected stale_statistics, got {names}"
    print("PASS: test_stale_statistics ->", [m.skill_name for m in matches])


def test_no_false_positive_on_healthy_plan():
    explain_json = [
        {
            "Plan": {
                "Node Type": "Index Scan",
                "Relation Name": "transactions",
                "Index Name": "idx_transactions_account_id",
                "Index Cond": "(account_id = 42)",
                "Plan Rows": 12,
                "Actual Rows": 11,
                "Total Cost": 8.5,
                "Actual Total Time": 0.05,
            },
            "Planning Time": 0.1,
            "Execution Time": 0.15,
        }
    ]
    plan = parse_explain_json(explain_json)
    matches = match_skills(plan, SKILLS)
    assert not matches, f"expected no matches on a healthy plan, got {[m.skill_name for m in matches]}"
    print("PASS: test_no_false_positive_on_healthy_plan -> []")


def test_plain_string_filter_is_not_flagged_as_cast():
    """
    Regression test for a real false positive found against a live
    database: `WHERE txn_type = 'OPER'` with no LOWER/UPPER/CAST at all.
    PostgreSQL's EXPLAIN output annotates the literal as 'OPER'::text —
    that's plan formatting, not a column-side cast, and must NOT trigger
    implicit_type_conversion.
    """
    explain_json = [
        {
            "Plan": {
                "Node Type": "Limit",
                "Plan Rows": 1,
                "Actual Rows": 7,
                "Total Cost": 12.5,
                "Actual Total Time": 0.02,
                "Plans": [
                    {
                        "Node Type": "Seq Scan",
                        "Relation Name": "transaction",
                        "Filter": "(txn_type = 'OPER'::text)",
                        "Plan Rows": 1,
                        "Actual Rows": 7,
                        "Total Cost": 12.5,
                        "Actual Total Time": 0.02,
                    }
                ],
            },
            "Planning Time": 0.85,
            "Execution Time": 0.03,
        }
    ]
    plan = parse_explain_json(explain_json)
    matches = match_skills(plan, SKILLS)
    names = {m.skill_name for m in matches}
    assert "implicit_type_conversion" not in names, (
        f"false positive regression: plain literal comparison flagged as cast, got {names}"
    )
    print("PASS: test_plain_string_filter_is_not_flagged_as_cast -> ", [m.skill_name for m in matches])


def test_real_upper_cast_is_still_flagged():
    """Make sure the fix didn't break detection of an ACTUAL column-side cast."""
    explain_json = [
        {
            "Plan": {
                "Node Type": "Seq Scan",
                "Relation Name": "customers",
                "Filter": "(upper((txn_type)::text) = 'OPER'::text)",
                "Plan Rows": 1,
                "Actual Rows": 7,
                "Total Cost": 12.5,
                "Actual Total Time": 0.02,
            },
            "Planning Time": 0.2,
            "Execution Time": 0.3,
        }
    ]
    plan = parse_explain_json(explain_json)
    matches = match_skills(plan, SKILLS)
    names = {m.skill_name for m in matches}
    assert "implicit_type_conversion" in names, f"expected real cast to still be caught, got {names}"
    print("PASS: test_real_upper_cast_is_still_flagged ->", [m.skill_name for m in matches])


def test_varchar_to_text_cast_is_not_flagged():
    """
    Regression test for a real false positive found against a live
    database: `txn_type` is `character varying`, so PostgreSQL's EXPLAIN
    shows the filter as `(transactions.txn_type)::text = 'OPER'::text`
    even though no LOWER/UPPER/TO_CHAR was used and index usage is NOT
    actually blocked. Must not be flagged as implicit_type_conversion.
    """
    explain_json = [
        {
            "Plan": {
                "Node Type": "Seq Scan",
                "Relation Name": "transactions",
                "Filter": "((transactions.txn_type)::text = 'OPER'::text)",
                "Plan Rows": 120207,
                "Actual Rows": 119884,
                "Total Cost": 4606.0,
                "Actual Total Time": 27.39,
            },
            "Planning Time": 0.97,
            "Execution Time": 30.65,
        }
    ]
    plan = parse_explain_json(explain_json)
    # Note: 119884 / 200000 ≈ 60% selectivity, so missing_index correctly
    # does NOT fire either (see test_low_selectivity_seq_scan_...). This
    # test only checks the cast false-positive is gone.
    matches = match_skills(plan, SKILLS, table_row_counts={"transactions": 200000})
    names = {m.skill_name for m in matches}
    assert "implicit_type_conversion" not in names, (
        f"false positive regression: varchar->text cast flagged as function-wrap, got {names}"
    )
    print("PASS: test_varchar_to_text_cast_is_not_flagged ->", [m.skill_name for m in matches])


def test_low_selectivity_seq_scan_not_flagged_even_without_index():
    """
    Regression test for the real-world case found during testing: a
    filter matching ~60% of a 200k-row table. Even with NO index at all,
    a sequential scan is often the objectively correct plan at that
    selectivity — an index lookup would mean random I/O for most of the
    table. missing_index must not fire here.
    """
    explain_json = [
        {
            "Plan": {
                "Node Type": "Seq Scan",
                "Relation Name": "transactions",
                "Filter": "((transactions.txn_type)::text = 'OPER'::text)",
                "Plan Rows": 120207,
                "Actual Rows": 119884,
                "Total Cost": 4606.0,
                "Actual Total Time": 27.58,
            },
            "Planning Time": 1.13,
            "Execution Time": 30.86,
        }
    ]
    plan = parse_explain_json(explain_json)
    # ~200k total rows, 119884 match -> ~60% selectivity -> should NOT be flagged
    matches = match_skills(plan, SKILLS, table_row_counts={"transactions": 200000})
    names = {m.skill_name for m in matches}
    assert "missing_index" not in names, (
        f"low-selectivity filter should not trigger missing_index, got {names}"
    )
    print("PASS: test_low_selectivity_seq_scan_not_flagged_even_without_index -> ", [m.skill_name for m in matches])


def test_high_selectivity_seq_scan_still_flagged():
    """Sanity check the fix didn't neuter the skill entirely: a filter
    matching only ~6% of a large table should still be flagged."""
    explain_json = [
        {
            "Plan": {
                "Node Type": "Seq Scan",
                "Relation Name": "transactions",
                "Filter": "(txn_type = 'ADJUST'::text)",
                "Plan Rows": 12000,
                "Actual Rows": 12000,
                "Total Cost": 4606.0,
                "Actual Total Time": 27.0,
            },
            "Planning Time": 1.0,
            "Execution Time": 28.0,
        }
    ]
    plan = parse_explain_json(explain_json)
    matches = match_skills(plan, SKILLS, table_row_counts={"transactions": 200000})
    names = {m.skill_name for m in matches}
    assert "missing_index" in names, f"expected high-selectivity filter to still trigger missing_index, got {names}"
    print("PASS: test_high_selectivity_seq_scan_still_flagged ->", [m.skill_name for m in matches])


if __name__ == "__main__":
    test_missing_index()
    test_implicit_conversion()
    test_stale_statistics()
    test_no_false_positive_on_healthy_plan()
    test_plain_string_filter_is_not_flagged_as_cast()
    test_real_upper_cast_is_still_flagged()
    test_varchar_to_text_cast_is_not_flagged()
    test_low_selectivity_seq_scan_not_flagged_even_without_index()
    test_high_selectivity_seq_scan_still_flagged()
    print("\nAll tests passed.")
