"""
Sanity tests that don't require a real database — they feed synthetic
EXPLAIN JSON straight into the parser and skill matcher, simulating what
psycopg2 would return for three classic anti-patterns.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.explain_parser import parse_explain_json
from core.schema_introspect import ColumnInfo, IndexInfo, TableSchema
from core.skill_matcher import CoverageStatus, LedgerStatus, Skill, load_skills, match_skills

_loaded = load_skills()
SKILLS = _loaded.skills


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
    result = match_skills(plan, SKILLS, table_row_counts={"transactions": 500000}, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "missing_index" in names, f"expected missing_index, got {names}"
    print("PASS: test_missing_index ->", [m.skill_name for m in result.matches])


def test_skill_match_carries_description():
    explain_json = [{
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
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, table_row_counts={"transactions": 500000}, ledger_status=LedgerStatus.OK)
    match = next(m for m in result.matches if m.skill_name == "missing_index")
    skill = next(s for s in SKILLS if s.name == "missing_index")
    assert match.description == skill.description, (
        f"SkillMatch.description should equal Skill.description, got {match.description!r}"
    )
    assert match.description.strip(), "SkillMatch.description must not be empty"


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
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "implicit_type_conversion" in names, f"expected implicit_type_conversion, got {names}"
    print("PASS: test_implicit_conversion ->", [m.skill_name for m in result.matches])


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
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "stale_statistics" in names, f"expected stale_statistics, got {names}"
    print("PASS: test_stale_statistics ->", [m.skill_name for m in result.matches])


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
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    assert not result.matches, f"expected no matches on a healthy plan, got {[m.skill_name for m in result.matches]}"
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
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "implicit_type_conversion" not in names, (
        f"false positive regression: plain literal comparison flagged as cast, got {names}"
    )
    print("PASS: test_plain_string_filter_is_not_flagged_as_cast -> ", [m.skill_name for m in result.matches])


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
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "implicit_type_conversion" in names, f"expected real cast to still be caught, got {names}"
    print("PASS: test_real_upper_cast_is_still_flagged ->", [m.skill_name for m in result.matches])


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
    result = match_skills(plan, SKILLS, table_row_counts={"transactions": 200000}, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "implicit_type_conversion" not in names, (
        f"false positive regression: varchar->text cast flagged as function-wrap, got {names}"
    )
    print("PASS: test_varchar_to_text_cast_is_not_flagged ->", [m.skill_name for m in result.matches])


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
    result = match_skills(plan, SKILLS, table_row_counts={"transactions": 200000}, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "missing_index" not in names, (
        f"low-selectivity filter should not trigger missing_index, got {names}"
    )
    print("PASS: test_low_selectivity_seq_scan_not_flagged_even_without_index -> ", [m.skill_name for m in result.matches])


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
    result = match_skills(plan, SKILLS, table_row_counts={"transactions": 200000}, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "missing_index" in names, f"expected high-selectivity filter to still trigger missing_index, got {names}"
    print("PASS: test_high_selectivity_seq_scan_still_flagged ->", [m.skill_name for m in result.matches])


def test_repeated_seq_scan_in_loop():
    """
    Simulates a classic correlated-subquery-in-disguise pattern: a
    Nested Loop where the inner Seq Scan runs once per outer row (5000
    loops), re-scanning a small unindexed lookup table each time.
    """
    explain_json = [
        {
            "Plan": {
                "Node Type": "Nested Loop",
                "Relation Name": None,
                "Plan Rows": 5000,
                "Actual Rows": 5000,
                "Total Cost": 55000.0,
                "Actual Total Time": 890.0,
                "Plans": [
                    {
                        "Node Type": "Seq Scan",
                        "Relation Name": "transactions",
                        "Plan Rows": 5000,
                        "Actual Rows": 5000,
                        "Total Cost": 90.0,
                        "Actual Total Time": 12.0,
                        "Actual Loops": 1,
                    },
                    {
                        "Node Type": "Seq Scan",
                        "Relation Name": "merchant_lookup",
                        "Filter": "(merchant_code = transactions.merchant_code)",
                        "Plan Rows": 1,
                        "Actual Rows": 1,
                        "Total Cost": 8.5,
                        "Actual Total Time": 0.15,
                        "Actual Loops": 5000,
                    },
                ],
            },
            "Planning Time": 0.4,
            "Execution Time": 891.0,
        }
    ]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, table_row_counts={"merchant_lookup": 5000}, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "repeated_seq_scan_in_loop" in names, f"expected repeated_seq_scan_in_loop, got {names}"
    print("PASS: test_repeated_seq_scan_in_loop ->", [m.skill_name for m in result.matches])


def test_no_applicable_skill_for_node_type_no_skill_covers():
    """
    A Bitmap Index Scan node: no skill covers this node type (not via "*" and
    not via explicit covers_node_types). Must produce NO_APPLICABLE_SKILL.
    Uses explicit_skills to exclude the two "*"-coverage skills (stale_statistics,
    empty_result_bad_estimate) so only deterministic per-type coverage remains.
    """
    explicit_skills = [s for s in SKILLS if "*" not in s.covers_node_types]
    explain_json = [
        {
            "Plan": {
                "Node Type": "Bitmap Heap Scan",
                "Relation Name": "transactions",
                "Plan Rows": 100,
                "Actual Rows": 105,
                "Total Cost": 100.0,
                "Actual Total Time": 5.0,
                "Plans": [{
                    "Node Type": "Bitmap Index Scan",
                    "Index Name": "idx_transactions_account_id",
                    "Plan Rows": 100,
                    "Actual Rows": 0,
                    "Total Cost": 4.0,
                    "Actual Total Time": 1.0,
                }],
            },
            "Planning Time": 0.1,
            "Execution Time": 5.1,
        }
    ]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, explicit_skills, ledger_status=LedgerStatus.OK)
    assert result.node_type_coverage.get("Bitmap Index Scan") == CoverageStatus.NO_APPLICABLE_SKILL


def test_skill_cleared_when_skill_covers_but_does_not_fire():
    """
    A Seq Scan at ~60% selectivity: missing_index covers Seq Scan but
    correctly doesn't fire. Coverage status must be SKILL_CLEARED — not
    silent — so the caller knows the node was examined, not just skipped.
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
    result = match_skills(plan, SKILLS, table_row_counts={"transactions": 200000}, ledger_status=LedgerStatus.OK)
    assert not result.matches
    assert result.node_type_coverage.get("Seq Scan") == CoverageStatus.SKILL_CLEARED


def test_single_loop_seq_scan_not_flagged_as_repeated():
    """A normal Seq Scan (loops=1) must not trigger the repeated-scan skill."""
    explain_json = [
        {
            "Plan": {
                "Node Type": "Seq Scan",
                "Relation Name": "transactions",
                "Plan Rows": 5000,
                "Actual Rows": 5000,
                "Total Cost": 90.0,
                "Actual Total Time": 12.0,
                "Actual Loops": 1,
            },
            "Planning Time": 0.1,
            "Execution Time": 12.1,
        }
    ]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, table_row_counts={"transactions": 5000}, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "repeated_seq_scan_in_loop" not in names, f"single-loop scan wrongly flagged, got {names}"
    print("PASS: test_single_loop_seq_scan_not_flagged_as_repeated ->", [m.skill_name for m in result.matches])


def test_hash_join_disk_spill():
    """
    Hash Join where the build side grew beyond work_mem and spilled to disk:
    Hash Batches (4) > Original Hash Batches (1). Must fire hash_join_disk_spill.
    """
    explain_json = [
        {
            "Plan": {
                "Node Type": "Hash Join",
                "Hash Cond": "(t.merchant_id = m.id)",
                "Plan Rows": 50000,
                "Actual Rows": 45000,
                "Total Cost": 8500.0,
                "Actual Total Time": 4200.0,
                "Plans": [
                    {
                        "Node Type": "Seq Scan",
                        "Relation Name": "transactions",
                        "Plan Rows": 50000,
                        "Actual Rows": 45000,
                        "Total Cost": 4000.0,
                        "Actual Total Time": 1200.0,
                    },
                    {
                        "Node Type": "Hash",
                        "Plan Rows": 1000,
                        "Actual Rows": 980,
                        "Total Cost": 200.0,
                        "Actual Total Time": 800.0,
                        "Hash Batches": 4,
                        "Original Hash Batches": 1,
                        "Hash Buckets": 1024,
                        "Original Hash Buckets": 1024,
                        "Peak Memory Usage": 8192,
                    },
                ],
            },
            "Planning Time": 1.5,
            "Execution Time": 4201.5,
        }
    ]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "hash_join_disk_spill" in names, f"expected hash_join_disk_spill, got {names}"


def test_no_false_positive_hash_join_no_spill():
    """Hash Join where build side fits in memory (Batches == Original == 1) — must not fire."""
    explain_json = [
        {
            "Plan": {
                "Node Type": "Hash Join",
                "Hash Cond": "(t.account_id = a.id)",
                "Plan Rows": 5000,
                "Actual Rows": 4800,
                "Total Cost": 850.0,
                "Actual Total Time": 42.0,
                "Plans": [
                    {
                        "Node Type": "Seq Scan",
                        "Relation Name": "transactions",
                        "Plan Rows": 5000,
                        "Actual Rows": 4800,
                        "Total Cost": 400.0,
                        "Actual Total Time": 22.0,
                    },
                    {
                        "Node Type": "Hash",
                        "Plan Rows": 1000,
                        "Actual Rows": 980,
                        "Total Cost": 200.0,
                        "Actual Total Time": 10.0,
                        "Hash Batches": 1,
                        "Original Hash Batches": 1,
                        "Hash Buckets": 1024,
                        "Original Hash Buckets": 1024,
                        "Peak Memory Usage": 256,
                    },
                ],
            },
            "Planning Time": 0.5,
            "Execution Time": 42.5,
        }
    ]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "hash_join_disk_spill" not in names, f"no-spill hash join wrongly flagged, got {names}"


def test_sort_spill_to_disk():
    """Sort node using external merge (disk spill) — must fire sort_spill_to_disk."""
    explain_json = [
        {
            "Plan": {
                "Node Type": "Sort",
                "Sort Key": ["created_at DESC"],
                "Sort Method": "external merge",
                "Sort Space Used": 102400,
                "Sort Space Type": "Disk",
                "Plan Rows": 500000,
                "Actual Rows": 500000,
                "Total Cost": 95000.0,
                "Actual Total Time": 8200.0,
                "Plans": [
                    {
                        "Node Type": "Seq Scan",
                        "Relation Name": "transactions",
                        "Plan Rows": 500000,
                        "Actual Rows": 500000,
                        "Total Cost": 45000.0,
                        "Actual Total Time": 2200.0,
                    }
                ],
            },
            "Planning Time": 0.8,
            "Execution Time": 8201.0,
        }
    ]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "sort_spill_to_disk" in names, f"expected sort_spill_to_disk, got {names}"


def test_no_false_positive_sort_in_memory():
    """Sort node using in-memory quicksort — must not be flagged."""
    explain_json = [
        {
            "Plan": {
                "Node Type": "Sort",
                "Sort Key": ["amount DESC"],
                "Sort Method": "quicksort",
                "Sort Space Used": 512,
                "Sort Space Type": "Memory",
                "Plan Rows": 1000,
                "Actual Rows": 1000,
                "Total Cost": 120.0,
                "Actual Total Time": 8.0,
                "Plans": [
                    {
                        "Node Type": "Seq Scan",
                        "Relation Name": "transactions",
                        "Plan Rows": 1000,
                        "Actual Rows": 1000,
                        "Total Cost": 100.0,
                        "Actual Total Time": 6.0,
                    }
                ],
            },
            "Planning Time": 0.3,
            "Execution Time": 8.3,
        }
    ]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "sort_spill_to_disk" not in names, f"in-memory sort wrongly flagged, got {names}"


def test_ratio_guard_allows_zero_actual_rows():
    """
    Guard fix: plan_rows > 0, actual_rows == 0 should NOT be blocked by the
    ratio guard. The guard exists to protect against plan_rows == 0 (undefined
    ratio), not against actual_rows == 0 (a genuinely empty result).
    Also confirms that plan_rows == 0 still returns False (guard stays intact).
    """
    from core.skill_matcher import Skill

    skill = Skill(
        name="_test_guard",
        description="",
        detects={"max_row_estimate_error_ratio": 0.1},
        severity="medium",
        explanation="",
        fix_template="",
        covers_node_types=[],
    )
    from core.explain_parser import PlanNode

    node_empty_result = PlanNode(
        node_type="Seq Scan", relation_name="t", index_name=None,
        filter_condition=None, index_condition=None,
        plan_rows=1000.0, actual_rows=0.0,
        total_cost=100.0, actual_total_time=5.0,
    )
    assert skill.matches_node(node_empty_result), (
        "guard wrongly blocked plan_rows=1000, actual_rows=0 (ratio=0.0 is meaningful here)"
    )

    node_zero_plan = PlanNode(
        node_type="Seq Scan", relation_name="t", index_name=None,
        filter_condition=None, index_condition=None,
        plan_rows=0.0, actual_rows=0.0,
        total_cost=100.0, actual_total_time=5.0,
    )
    assert not skill.matches_node(node_zero_plan), (
        "guard must still block plan_rows=0 (ratio is undefined)"
    )


def test_empty_result_bad_estimate():
    """
    Seq Scan estimated 1000 rows but returned 0 — planner badly overestimated.
    empty_result_bad_estimate must fire.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "transactions",
            "Filter": "(account_id = 9999 AND status = 'DELETED')",
            "Plan Rows": 1000,
            "Actual Rows": 0,
            "Total Cost": 4000.0,
            "Actual Total Time": 25.0,
        },
        "Planning Time": 0.3,
        "Execution Time": 25.3,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "empty_result_bad_estimate" in names, (
        f"expected empty_result_bad_estimate, got {names}"
    )


def test_no_false_positive_empty_result_plan_too_small():
    """Plan rows below the min_plan_rows threshold — not worth flagging."""
    explain_json = [{
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "transactions",
            "Plan Rows": 5,
            "Actual Rows": 0,
            "Total Cost": 10.0,
            "Actual Total Time": 0.5,
        },
        "Planning Time": 0.1,
        "Execution Time": 0.6,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "empty_result_bad_estimate" not in names, (
        f"plan_rows=5 is below min threshold, should not fire, got {names}"
    )


def test_no_false_positive_empty_result_ratio_too_high():
    """Plan rows and actual rows are close — not a bad estimate."""
    explain_json = [{
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "transactions",
            "Plan Rows": 1000,
            "Actual Rows": 800,
            "Total Cost": 4000.0,
            "Actual Total Time": 25.0,
        },
        "Planning Time": 0.3,
        "Execution Time": 25.3,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "empty_result_bad_estimate" not in names, (
        f"ratio=0.8 is above threshold, should not fire, got {names}"
    )


def test_heap_fetches_parsed_from_explain_json():
    """Heap Fetches from EXPLAIN JSON must be captured on PlanNode.heap_fetches."""
    explain_json = [{
        "Plan": {
            "Node Type": "Index Only Scan",
            "Relation Name": "transactions",
            "Index Name": "idx_transactions_account_id",
            "Index Cond": "(account_id = 42)",
            "Heap Fetches": 4823,
            "Plan Rows": 5000,
            "Actual Rows": 5000,
            "Total Cost": 200.0,
            "Actual Total Time": 45.0,
        },
        "Planning Time": 0.2,
        "Execution Time": 45.2,
    }]
    plan = parse_explain_json(explain_json)
    node = plan.root
    assert node.heap_fetches == 4823, (
        f"expected heap_fetches=4823, got {node.heap_fetches!r}"
    )


def test_index_only_scan_heap_fetches():
    """Index Only Scan with heap_fetches > 50% of actual_rows — must fire."""
    explain_json = [{
        "Plan": {
            "Node Type": "Index Only Scan",
            "Relation Name": "transactions",
            "Index Name": "idx_transactions_account_id",
            "Index Cond": "(account_id = 42)",
            "Heap Fetches": 4823,
            "Plan Rows": 5000,
            "Actual Rows": 5000,
            "Total Cost": 200.0,
            "Actual Total Time": 45.0,
        },
        "Planning Time": 0.2,
        "Execution Time": 45.2,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "index_only_scan_heap_fetches" in names, (
        f"expected index_only_scan_heap_fetches, got {names}"
    )


def test_no_false_positive_heap_fetches_low_ratio():
    """Index Only Scan with few heap fetches relative to rows — well below threshold."""
    explain_json = [{
        "Plan": {
            "Node Type": "Index Only Scan",
            "Relation Name": "transactions",
            "Index Name": "idx_transactions_account_id",
            "Index Cond": "(account_id = 42)",
            "Heap Fetches": 50,
            "Plan Rows": 5000,
            "Actual Rows": 5000,
            "Total Cost": 200.0,
            "Actual Total Time": 10.0,
        },
        "Planning Time": 0.2,
        "Execution Time": 10.2,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "index_only_scan_heap_fetches" not in names, (
        f"low ratio (50/5000=1%) should not fire, got {names}"
    )


def test_no_false_positive_heap_fetches_zero():
    """Index Only Scan with zero heap fetches — visibility map fully covered."""
    explain_json = [{
        "Plan": {
            "Node Type": "Index Only Scan",
            "Relation Name": "transactions",
            "Index Name": "idx_transactions_account_id",
            "Index Cond": "(account_id = 42)",
            "Heap Fetches": 0,
            "Plan Rows": 5000,
            "Actual Rows": 5000,
            "Total Cost": 180.0,
            "Actual Total Time": 8.0,
        },
        "Planning Time": 0.2,
        "Execution Time": 8.2,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "index_only_scan_heap_fetches" not in names, (
        f"zero heap fetches should not fire, got {names}"
    )


def test_sort_key_parsed_from_explain_json():
    """Sort Key array from EXPLAIN JSON must be captured on PlanNode.sort_key."""
    explain_json = [{
        "Plan": {
            "Node Type": "Sort",
            "Sort Key": ["created_at DESC", "id"],
            "Sort Method": "quicksort",
            "Sort Space Used": 256,
            "Sort Space Type": "Memory",
            "Plan Rows": 100,
            "Actual Rows": 100,
            "Total Cost": 50.0,
            "Actual Total Time": 2.0,
            "Plans": [{
                "Node Type": "Seq Scan",
                "Relation Name": "transactions",
                "Plan Rows": 100,
                "Actual Rows": 100,
                "Total Cost": 40.0,
                "Actual Total Time": 1.5,
            }],
        },
        "Planning Time": 0.1,
        "Execution Time": 2.1,
    }]
    plan = parse_explain_json(explain_json)
    sort_node = plan.root
    assert sort_node.sort_key == ["created_at DESC", "id"], (
        f"expected sort_key=['created_at DESC', 'id'], got {sort_node.sort_key!r}"
    )


def test_redundant_sort_after_ordered_scan():
    """Sort over Index Scan where sort key matches index leading column — skill fires."""
    schema = {
        "transactions": TableSchema(
            table_name="transactions",
            indexes=[IndexInfo(
                name="idx_transactions_account_id",
                definition="CREATE INDEX idx_transactions_account_id ON public.transactions USING btree (account_id)",
            )],
        )
    }
    explain_json = [{
        "Plan": {
            "Node Type": "Sort",
            "Sort Key": ["account_id"],
            "Sort Method": "quicksort",
            "Sort Space Used": 512,
            "Sort Space Type": "Memory",
            "Plan Rows": 1000,
            "Actual Rows": 1000,
            "Total Cost": 5000.0,
            "Actual Total Time": 100.0,
            "Plans": [{
                "Node Type": "Index Scan",
                "Relation Name": "transactions",
                "Index Name": "idx_transactions_account_id",
                "Plan Rows": 1000,
                "Actual Rows": 1000,
                "Total Cost": 4000.0,
                "Actual Total Time": 80.0,
            }],
        },
        "Planning Time": 0.3,
        "Execution Time": 100.3,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK, schema_context=schema)
    names = {m.skill_name for m in result.matches}
    assert "redundant_sort_after_ordered_scan" in names, (
        f"expected redundant_sort_after_ordered_scan, got {names}"
    )


def test_no_false_positive_sort_over_seq_scan():
    """Sort on top of a Seq Scan is NOT a redundant sort — Seq Scan output is unordered."""
    explain_json = [{
        "Plan": {
            "Node Type": "Sort",
            "Sort Key": ["account_id"],
            "Sort Method": "quicksort",
            "Sort Space Used": 512,
            "Sort Space Type": "Memory",
            "Plan Rows": 1000,
            "Actual Rows": 1000,
            "Total Cost": 5000.0,
            "Actual Total Time": 100.0,
            "Plans": [{
                "Node Type": "Seq Scan",
                "Relation Name": "transactions",
                "Plan Rows": 1000,
                "Actual Rows": 1000,
                "Total Cost": 4000.0,
                "Actual Total Time": 80.0,
            }],
        },
        "Planning Time": 0.3,
        "Execution Time": 100.3,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "redundant_sort_after_ordered_scan" not in names, (
        f"Sort over Seq Scan wrongly flagged as redundant, got {names}"
    )


def test_no_false_positive_redundant_sort_mismatched_key():
    """
    v1 false positive: Sort → Index Scan on (account_id) but ORDER BY amount DESC.
    The index doesn't cover the sort key — the Sort is necessary, not redundant.
    v2 must NOT fire because sort_key_matches_child_index rejects the mismatch.
    """
    schema = {
        "transactions": TableSchema(
            table_name="transactions",
            indexes=[IndexInfo(
                name="idx_transactions_account_id",
                definition="CREATE INDEX idx_transactions_account_id ON public.transactions USING btree (account_id)",
            )],
        )
    }
    explain_json = [{
        "Plan": {
            "Node Type": "Sort",
            "Sort Key": ["amount DESC"],
            "Sort Method": "quicksort",
            "Sort Space Used": 1024,
            "Sort Space Type": "Memory",
            "Plan Rows": 1000,
            "Actual Rows": 1000,
            "Total Cost": 5000.0,
            "Actual Total Time": 110.0,
            "Plans": [{
                "Node Type": "Index Scan",
                "Relation Name": "transactions",
                "Index Name": "idx_transactions_account_id",
                "Index Cond": "(account_id = 12345)",
                "Plan Rows": 1000,
                "Actual Rows": 1000,
                "Total Cost": 4000.0,
                "Actual Total Time": 85.0,
            }],
        },
        "Planning Time": 0.3,
        "Execution Time": 110.3,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK, schema_context=schema)
    names = {m.skill_name for m in result.matches}
    assert "redundant_sort_after_ordered_scan" not in names, (
        "Sort key 'amount DESC' doesn't match index on (account_id) — "
        "sort is necessary, must not fire, got {names}"
    )


def test_no_false_positive_redundant_sort_no_schema_context():
    """
    Shape matches (Sort → Index Scan) but schema_context is absent — skill
    must abstain rather than fire as a v1-style shape heuristic. A schema-
    dependent skill must not produce findings when it can't verify them.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Sort",
            "Sort Key": ["account_id"],
            "Sort Method": "quicksort",
            "Sort Space Used": 512,
            "Sort Space Type": "Memory",
            "Plan Rows": 1000,
            "Actual Rows": 1000,
            "Total Cost": 5000.0,
            "Actual Total Time": 100.0,
            "Plans": [{
                "Node Type": "Index Scan",
                "Relation Name": "transactions",
                "Index Name": "idx_transactions_account_id",
                "Plan Rows": 1000,
                "Actual Rows": 1000,
                "Total Cost": 4000.0,
                "Actual Total Time": 80.0,
            }],
        },
        "Planning Time": 0.3,
        "Execution Time": 100.3,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK, schema_context=None)
    names = {m.skill_name for m in result.matches}
    assert "redundant_sort_after_ordered_scan" not in names, (
        f"skill must abstain when schema_context is absent, got {names}"
    )


def test_redundant_sort_multicolumn_match():
    """Sort key (account_id, created_at) is a prefix of a 3-column composite index — skill fires."""
    schema = {
        "transactions": TableSchema(
            table_name="transactions",
            indexes=[IndexInfo(
                name="idx_transactions_composite",
                definition="CREATE INDEX idx_transactions_composite ON public.transactions USING btree (account_id, created_at, amount)",
            )],
        )
    }
    explain_json = [{
        "Plan": {
            "Node Type": "Sort",
            "Sort Key": ["account_id", "created_at"],
            "Sort Method": "quicksort",
            "Plan Rows": 500,
            "Actual Rows": 500,
            "Total Cost": 3000.0,
            "Actual Total Time": 60.0,
            "Plans": [{
                "Node Type": "Index Scan",
                "Relation Name": "transactions",
                "Index Name": "idx_transactions_composite",
                "Plan Rows": 500,
                "Actual Rows": 500,
                "Total Cost": 2500.0,
                "Actual Total Time": 50.0,
            }],
        },
        "Planning Time": 0.2,
        "Execution Time": 60.2,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK, schema_context=schema)
    names = {m.skill_name for m in result.matches}
    assert "redundant_sort_after_ordered_scan" in names, (
        f"expected redundant_sort_after_ordered_scan for prefix match, got {names}"
    )


def test_redundant_sort_desc_match():
    """Sort key (amount DESC) matches an index defined as (amount DESC) — skill fires."""
    schema = {
        "transactions": TableSchema(
            table_name="transactions",
            indexes=[IndexInfo(
                name="idx_transactions_amount_desc",
                definition="CREATE INDEX idx_transactions_amount_desc ON public.transactions USING btree (amount DESC)",
            )],
        )
    }
    explain_json = [{
        "Plan": {
            "Node Type": "Sort",
            "Sort Key": ["amount DESC"],
            "Sort Method": "quicksort",
            "Plan Rows": 800,
            "Actual Rows": 800,
            "Total Cost": 4200.0,
            "Actual Total Time": 75.0,
            "Plans": [{
                "Node Type": "Index Scan",
                "Relation Name": "transactions",
                "Index Name": "idx_transactions_amount_desc",
                "Plan Rows": 800,
                "Actual Rows": 800,
                "Total Cost": 3800.0,
                "Actual Total Time": 65.0,
            }],
        },
        "Planning Time": 0.2,
        "Execution Time": 75.2,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK, schema_context=schema)
    names = {m.skill_name for m in result.matches}
    assert "redundant_sort_after_ordered_scan" in names, (
        f"expected redundant_sort_after_ordered_scan for DESC match, got {names}"
    )


def test_redundant_sort_index_only_scan():
    """Sort over Index Only Scan where sort key matches index — skill fires."""
    schema = {
        "transactions": TableSchema(
            table_name="transactions",
            indexes=[IndexInfo(
                name="idx_transactions_account_id",
                definition="CREATE INDEX idx_transactions_account_id ON public.transactions USING btree (account_id)",
            )],
        )
    }
    explain_json = [{
        "Plan": {
            "Node Type": "Sort",
            "Sort Key": ["account_id"],
            "Sort Method": "quicksort",
            "Plan Rows": 1000,
            "Actual Rows": 1000,
            "Total Cost": 5000.0,
            "Actual Total Time": 90.0,
            "Plans": [{
                "Node Type": "Index Only Scan",
                "Relation Name": "transactions",
                "Index Name": "idx_transactions_account_id",
                "Heap Fetches": 0,
                "Plan Rows": 1000,
                "Actual Rows": 1000,
                "Total Cost": 4500.0,
                "Actual Total Time": 80.0,
            }],
        },
        "Planning Time": 0.2,
        "Execution Time": 90.2,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK, schema_context=schema)
    names = {m.skill_name for m in result.matches}
    assert "redundant_sort_after_ordered_scan" in names, (
        f"expected redundant_sort_after_ordered_scan for Index Only Scan, got {names}"
    )


def test_no_false_positive_redundant_sort_direction_mismatch():
    """Sort key (amount ASC) vs index defined as (amount DESC) — directions differ, must NOT fire."""
    schema = {
        "transactions": TableSchema(
            table_name="transactions",
            indexes=[IndexInfo(
                name="idx_transactions_amount_desc",
                definition="CREATE INDEX idx_transactions_amount_desc ON public.transactions USING btree (amount DESC)",
            )],
        )
    }
    explain_json = [{
        "Plan": {
            "Node Type": "Sort",
            "Sort Key": ["amount"],
            "Sort Method": "quicksort",
            "Plan Rows": 800,
            "Actual Rows": 800,
            "Total Cost": 4200.0,
            "Actual Total Time": 75.0,
            "Plans": [{
                "Node Type": "Index Scan",
                "Relation Name": "transactions",
                "Index Name": "idx_transactions_amount_desc",
                "Plan Rows": 800,
                "Actual Rows": 800,
                "Total Cost": 3800.0,
                "Actual Total Time": 65.0,
            }],
        },
        "Planning Time": 0.2,
        "Execution Time": 75.2,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK, schema_context=schema)
    names = {m.skill_name for m in result.matches}
    assert "redundant_sort_after_ordered_scan" not in names, (
        f"Sort ASC vs index DESC — sort is necessary, must not fire, got {names}"
    )


def test_nested_loop_bad_plan():
    """
    Nested Loop where the outer child's row estimate was 100x off — the planner
    expected 5 outer rows (loop iterations) but got 50,000. Inner child is an
    Index Scan, confirming the planner chose index lookup on the inner side.
    nested_loop_bad_plan must fire.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Nested Loop",
            "Plan Rows": 5,
            "Actual Rows": 50000,
            "Total Cost": 100000.0,
            "Actual Total Time": 5000.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "customers",
                    "Plan Rows": 5,
                    "Actual Rows": 50000,
                    "Total Cost": 5000.0,
                    "Actual Total Time": 200.0,
                    "Actual Loops": 1,
                },
                {
                    "Node Type": "Index Scan",
                    "Relation Name": "transactions",
                    "Index Name": "idx_transactions_customer_id",
                    "Index Cond": "(customer_id = customers.id)",
                    "Plan Rows": 1,
                    "Actual Rows": 1,
                    "Total Cost": 1.5,
                    "Actual Total Time": 0.05,
                    "Actual Loops": 50000,
                },
            ],
        },
        "Planning Time": 0.5,
        "Execution Time": 5000.5,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "nested_loop_bad_plan" in names, (
        f"expected nested_loop_bad_plan (outer ratio=10000x), got {names}"
    )


def test_no_false_positive_nested_loop_good_estimate():
    """
    Nested Loop where the outer child's estimate was accurate (~1x off).
    The inner Index Scan confirms the shape but the outer estimate is fine —
    nested_loop_bad_plan must not fire.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Nested Loop",
            "Plan Rows": 100,
            "Actual Rows": 110,
            "Total Cost": 500.0,
            "Actual Total Time": 20.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "customers",
                    "Plan Rows": 100,
                    "Actual Rows": 110,
                    "Total Cost": 200.0,
                    "Actual Total Time": 8.0,
                    "Actual Loops": 1,
                },
                {
                    "Node Type": "Index Scan",
                    "Relation Name": "transactions",
                    "Index Name": "idx_transactions_customer_id",
                    "Index Cond": "(customer_id = customers.id)",
                    "Plan Rows": 1,
                    "Actual Rows": 1,
                    "Total Cost": 1.5,
                    "Actual Total Time": 0.1,
                    "Actual Loops": 110,
                },
            ],
        },
        "Planning Time": 0.2,
        "Execution Time": 20.2,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "nested_loop_bad_plan" not in names, (
        f"outer ratio ~1.1x should not fire nested_loop_bad_plan, got {names}"
    )


def test_no_false_positive_nested_loop_seq_scan_inner():
    """
    Nested Loop where the outer child is badly underestimated but the inner
    child is a Seq Scan (not an Index Scan). Shape check fails — nested_loop_bad_plan
    only applies when the planner chose an Index Scan for the inner side.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Nested Loop",
            "Plan Rows": 5,
            "Actual Rows": 50000,
            "Total Cost": 200000.0,
            "Actual Total Time": 10000.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "customers",
                    "Plan Rows": 5,
                    "Actual Rows": 50000,
                    "Total Cost": 5000.0,
                    "Actual Total Time": 200.0,
                    "Actual Loops": 1,
                },
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions",
                    "Filter": "(customer_id = customers.id)",
                    "Plan Rows": 1,
                    "Actual Rows": 1,
                    "Total Cost": 3.0,
                    "Actual Total Time": 0.15,
                    "Actual Loops": 50000,
                },
            ],
        },
        "Planning Time": 0.5,
        "Execution Time": 10000.5,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "nested_loop_bad_plan" not in names, (
        f"Seq Scan inner child should not trigger nested_loop_bad_plan, got {names}"
    )


def test_workers_parsed_from_explain_json():
    """Workers Planned and Workers Launched must be captured on Gather PlanNode."""
    explain_json = [{
        "Plan": {
            "Node Type": "Gather",
            "Workers Planned": 4,
            "Workers Launched": 2,
            "Plan Rows": 100000,
            "Actual Rows": 100000,
            "Total Cost": 50000.0,
            "Actual Total Time": 2000.0,
            "Plans": [{
                "Node Type": "Seq Scan",
                "Relation Name": "transactions",
                "Plan Rows": 25000,
                "Actual Rows": 50000,
                "Total Cost": 10000.0,
                "Actual Total Time": 1800.0,
                "Actual Loops": 2,
            }],
        },
        "Planning Time": 0.5,
        "Execution Time": 2000.5,
    }]
    plan = parse_explain_json(explain_json)
    node = plan.root
    assert node.workers_planned == 4, f"expected workers_planned=4, got {node.workers_planned!r}"
    assert node.workers_launched == 2, f"expected workers_launched=2, got {node.workers_launched!r}"


def test_parallel_worker_underutilization():
    """
    Gather with workers_planned=4 but workers_launched=2 — server couldn't
    provide all requested workers. parallel_worker_underutilization must fire.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Gather",
            "Workers Planned": 4,
            "Workers Launched": 2,
            "Plan Rows": 100000,
            "Actual Rows": 100000,
            "Total Cost": 50000.0,
            "Actual Total Time": 2000.0,
            "Plans": [{
                "Node Type": "Seq Scan",
                "Relation Name": "transactions",
                "Plan Rows": 25000,
                "Actual Rows": 50000,
                "Total Cost": 10000.0,
                "Actual Total Time": 1800.0,
                "Actual Loops": 2,
            }],
        },
        "Planning Time": 0.5,
        "Execution Time": 2000.5,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "parallel_worker_underutilization" in names, (
        f"expected parallel_worker_underutilization (2 of 4 workers), got {names}"
    )


def test_no_false_positive_parallel_full_workers():
    """Gather where all planned workers launched — no shortfall, must not fire."""
    explain_json = [{
        "Plan": {
            "Node Type": "Gather",
            "Workers Planned": 4,
            "Workers Launched": 4,
            "Plan Rows": 100000,
            "Actual Rows": 100000,
            "Total Cost": 50000.0,
            "Actual Total Time": 800.0,
            "Plans": [{
                "Node Type": "Seq Scan",
                "Relation Name": "transactions",
                "Plan Rows": 25000,
                "Actual Rows": 25000,
                "Total Cost": 10000.0,
                "Actual Total Time": 700.0,
                "Actual Loops": 4,
            }],
        },
        "Planning Time": 0.5,
        "Execution Time": 800.5,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "parallel_worker_underutilization" not in names, (
        f"all 4 workers launched, should not fire, got {names}"
    )


def test_no_false_positive_gather_merge_full_workers():
    """Gather Merge where all planned workers launched — must not fire."""
    explain_json = [{
        "Plan": {
            "Node Type": "Gather Merge",
            "Workers Planned": 2,
            "Workers Launched": 2,
            "Plan Rows": 50000,
            "Actual Rows": 50000,
            "Total Cost": 30000.0,
            "Actual Total Time": 600.0,
            "Plans": [{
                "Node Type": "Sort",
                "Sort Key": ["amount DESC"],
                "Sort Method": "quicksort",
                "Sort Space Used": 512,
                "Sort Space Type": "Memory",
                "Plan Rows": 25000,
                "Actual Rows": 25000,
                "Total Cost": 14000.0,
                "Actual Total Time": 550.0,
                "Actual Loops": 2,
            }],
        },
        "Planning Time": 0.4,
        "Execution Time": 600.4,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "parallel_worker_underutilization" not in names, (
        f"Gather Merge with full workers should not fire, got {names}"
    )


def test_repeated_index_scan_in_loop():
    """
    Index Scan on accounts running 5000 times as the inner side of a Nested
    Loop — individually cheap, cumulatively expensive. repeated_index_scan_in_loop
    must fire. Outer estimate is accurate so nested_loop_bad_plan stays silent.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Nested Loop",
            "Plan Rows": 5000,
            "Actual Rows": 5000,
            "Total Cost": 30000.0,
            "Actual Total Time": 450.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions",
                    "Plan Rows": 5000,
                    "Actual Rows": 5000,
                    "Total Cost": 100.0,
                    "Actual Total Time": 12.0,
                    "Actual Loops": 1,
                },
                {
                    "Node Type": "Index Scan",
                    "Relation Name": "accounts",
                    "Index Name": "idx_accounts_id",
                    "Index Cond": "(id = transactions.account_id)",
                    "Plan Rows": 1,
                    "Actual Rows": 1,
                    "Total Cost": 4.0,
                    "Actual Total Time": 0.08,
                    "Actual Loops": 5000,
                },
            ],
        },
        "Planning Time": 0.3,
        "Execution Time": 450.3,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "repeated_index_scan_in_loop" in names, (
        f"expected repeated_index_scan_in_loop (5000 loops), got {names}"
    )


def test_repeated_index_only_scan_in_loop():
    """
    Index Only Scan running 5000 times — same pattern as repeated_index_scan_in_loop
    but for the Index Only Scan variant. Verifies list membership check works for
    the second entry in node_type: ["Index Scan", "Index Only Scan"].
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Nested Loop",
            "Plan Rows": 5000,
            "Actual Rows": 5000,
            "Total Cost": 25000.0,
            "Actual Total Time": 380.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions",
                    "Plan Rows": 5000,
                    "Actual Rows": 5000,
                    "Total Cost": 100.0,
                    "Actual Total Time": 12.0,
                    "Actual Loops": 1,
                },
                {
                    "Node Type": "Index Only Scan",
                    "Relation Name": "accounts",
                    "Index Name": "idx_accounts_id_balance",
                    "Index Cond": "(id = transactions.account_id)",
                    "Heap Fetches": 0,
                    "Plan Rows": 1,
                    "Actual Rows": 1,
                    "Total Cost": 3.0,
                    "Actual Total Time": 0.06,
                    "Actual Loops": 5000,
                },
            ],
        },
        "Planning Time": 0.3,
        "Execution Time": 380.3,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "repeated_index_scan_in_loop" in names, (
        f"expected repeated_index_scan_in_loop for Index Only Scan (5000 loops), got {names}"
    )


def test_no_false_positive_index_scan_low_loops():
    """
    Index Scan running only 10 times — well below the 50-loop threshold.
    repeated_index_scan_in_loop must not fire.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Nested Loop",
            "Plan Rows": 10,
            "Actual Rows": 10,
            "Total Cost": 80.0,
            "Actual Total Time": 2.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions",
                    "Plan Rows": 10,
                    "Actual Rows": 10,
                    "Total Cost": 40.0,
                    "Actual Total Time": 1.0,
                    "Actual Loops": 1,
                },
                {
                    "Node Type": "Index Scan",
                    "Relation Name": "accounts",
                    "Index Name": "idx_accounts_id",
                    "Index Cond": "(id = transactions.account_id)",
                    "Plan Rows": 1,
                    "Actual Rows": 1,
                    "Total Cost": 4.0,
                    "Actual Total Time": 0.08,
                    "Actual Loops": 10,
                },
            ],
        },
        "Planning Time": 0.1,
        "Execution Time": 2.1,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "repeated_index_scan_in_loop" not in names, (
        f"10 loops is below threshold, should not fire, got {names}"
    )


def test_join_condition_function_wrap_hash_join():
    """
    Hash Join with LOWER() wrapped around both join keys — planner can't use
    an index on the join column. join_condition_function_wrap must fire.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Hash Join",
            "Hash Cond": "(lower(transactions.merchant_name) = lower(merchants.name))",
            "Plan Rows": 5000,
            "Actual Rows": 4800,
            "Total Cost": 10000.0,
            "Actual Total Time": 350.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions",
                    "Plan Rows": 5000,
                    "Actual Rows": 5000,
                    "Total Cost": 500.0,
                    "Actual Total Time": 50.0,
                },
                {
                    "Node Type": "Hash",
                    "Plan Rows": 1000,
                    "Actual Rows": 1000,
                    "Total Cost": 100.0,
                    "Actual Total Time": 10.0,
                    "Plans": [{
                        "Node Type": "Seq Scan",
                        "Relation Name": "merchants",
                        "Plan Rows": 1000,
                        "Actual Rows": 1000,
                        "Total Cost": 80.0,
                        "Actual Total Time": 8.0,
                    }],
                },
            ],
        },
        "Planning Time": 0.5,
        "Execution Time": 350.5,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "join_condition_function_wrap" in names, (
        f"expected join_condition_function_wrap (LOWER in Hash Cond), got {names}"
    )


def test_join_condition_function_wrap_merge_join():
    """
    Merge Join with UPPER() in Merge Cond — verifies list membership check for
    the second node_type entry and that merge_cond is in the haystack.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Merge Join",
            "Merge Cond": "(upper(a.code) = upper(b.code))",
            "Plan Rows": 2000,
            "Actual Rows": 1900,
            "Total Cost": 8000.0,
            "Actual Total Time": 220.0,
            "Plans": [
                {
                    "Node Type": "Sort",
                    "Sort Key": ["upper(a.code)"],
                    "Sort Method": "quicksort",
                    "Sort Space Used": 256,
                    "Sort Space Type": "Memory",
                    "Plan Rows": 2000,
                    "Actual Rows": 2000,
                    "Total Cost": 4000.0,
                    "Actual Total Time": 100.0,
                    "Plans": [{
                        "Node Type": "Seq Scan",
                        "Relation Name": "a",
                        "Plan Rows": 2000,
                        "Actual Rows": 2000,
                        "Total Cost": 200.0,
                        "Actual Total Time": 20.0,
                    }],
                },
                {
                    "Node Type": "Sort",
                    "Sort Key": ["upper(b.code)"],
                    "Sort Method": "quicksort",
                    "Sort Space Used": 128,
                    "Sort Space Type": "Memory",
                    "Plan Rows": 500,
                    "Actual Rows": 500,
                    "Total Cost": 1000.0,
                    "Actual Total Time": 30.0,
                    "Plans": [{
                        "Node Type": "Seq Scan",
                        "Relation Name": "b",
                        "Plan Rows": 500,
                        "Actual Rows": 500,
                        "Total Cost": 80.0,
                        "Actual Total Time": 8.0,
                    }],
                },
            ],
        },
        "Planning Time": 0.4,
        "Execution Time": 220.4,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "join_condition_function_wrap" in names, (
        f"expected join_condition_function_wrap (UPPER in Merge Cond), got {names}"
    )


def test_no_false_positive_join_plain_column_condition():
    """Hash Join with a plain column equality — no function wrap, must not fire."""
    explain_json = [{
        "Plan": {
            "Node Type": "Hash Join",
            "Hash Cond": "(transactions.account_id = accounts.id)",
            "Plan Rows": 5000,
            "Actual Rows": 5000,
            "Total Cost": 8000.0,
            "Actual Total Time": 150.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions",
                    "Plan Rows": 5000,
                    "Actual Rows": 5000,
                    "Total Cost": 500.0,
                    "Actual Total Time": 50.0,
                },
                {
                    "Node Type": "Hash",
                    "Plan Rows": 1000,
                    "Actual Rows": 1000,
                    "Total Cost": 100.0,
                    "Actual Total Time": 10.0,
                    "Plans": [{
                        "Node Type": "Seq Scan",
                        "Relation Name": "accounts",
                        "Plan Rows": 1000,
                        "Actual Rows": 1000,
                        "Total Cost": 80.0,
                        "Actual Total Time": 8.0,
                    }],
                },
            ],
        },
        "Planning Time": 0.3,
        "Execution Time": 150.3,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "join_condition_function_wrap" not in names, (
        f"plain column join should not fire, got {names}"
    )


def test_join_conditions_parsed_from_explain_json():
    """Hash Cond and Merge Cond must be captured on their respective PlanNodes."""
    hash_json = [{
        "Plan": {
            "Node Type": "Hash Join",
            "Hash Cond": "(lower(transactions.merchant_name) = lower(merchants.name))",
            "Plan Rows": 5000,
            "Actual Rows": 5000,
            "Total Cost": 10000.0,
            "Actual Total Time": 200.0,
            "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": "transactions",
                 "Plan Rows": 5000, "Actual Rows": 5000,
                 "Total Cost": 500.0, "Actual Total Time": 50.0},
                {"Node Type": "Hash", "Plan Rows": 1000, "Actual Rows": 1000,
                 "Total Cost": 100.0, "Actual Total Time": 10.0,
                 "Plans": [{"Node Type": "Seq Scan", "Relation Name": "merchants",
                             "Plan Rows": 1000, "Actual Rows": 1000,
                             "Total Cost": 80.0, "Actual Total Time": 8.0}]},
            ],
        },
        "Planning Time": 0.5,
        "Execution Time": 200.5,
    }]
    plan = parse_explain_json(hash_json)
    node = plan.root
    assert node.hash_cond == "(lower(transactions.merchant_name) = lower(merchants.name))", (
        f"expected hash_cond to be set, got {node.hash_cond!r}"
    )
    assert node.merge_cond is None, f"expected merge_cond=None on Hash Join, got {node.merge_cond!r}"


def test_hash_join_build_probe_imbalance():
    """
    Hash Join where the build side (Hash node, children[1]) has 50,000 rows
    but the probe side (children[0]) has only 5,000 rows — 10x ratio, well
    above the 5x threshold. hash_join_build_probe_imbalance must fire.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Hash Join",
            "Hash Cond": "(transactions.account_id = large_ref.id)",
            "Plan Rows": 5000,
            "Actual Rows": 5000,
            "Total Cost": 20000.0,
            "Actual Total Time": 500.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions",
                    "Plan Rows": 5000,
                    "Actual Rows": 5000,
                    "Total Cost": 500.0,
                    "Actual Total Time": 50.0,
                },
                {
                    "Node Type": "Hash",
                    "Plan Rows": 50000,
                    "Actual Rows": 50000,
                    "Total Cost": 5000.0,
                    "Actual Total Time": 100.0,
                    "Plans": [{
                        "Node Type": "Seq Scan",
                        "Relation Name": "large_ref",
                        "Plan Rows": 50000,
                        "Actual Rows": 50000,
                        "Total Cost": 4000.0,
                        "Actual Total Time": 80.0,
                    }],
                },
            ],
        },
        "Planning Time": 0.5,
        "Execution Time": 500.5,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "hash_join_build_probe_imbalance" in names, (
        f"expected hash_join_build_probe_imbalance (build=50k, probe=5k, ratio=10x), got {names}"
    )


def test_no_false_positive_hash_join_balanced():
    """
    Hash Join where both sides have similar row counts (~1x ratio).
    hash_join_build_probe_imbalance must not fire.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Hash Join",
            "Hash Cond": "(transactions.account_id = accounts.id)",
            "Plan Rows": 5000,
            "Actual Rows": 5000,
            "Total Cost": 10000.0,
            "Actual Total Time": 200.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions",
                    "Plan Rows": 5000,
                    "Actual Rows": 5000,
                    "Total Cost": 500.0,
                    "Actual Total Time": 50.0,
                },
                {
                    "Node Type": "Hash",
                    "Plan Rows": 4800,
                    "Actual Rows": 4800,
                    "Total Cost": 500.0,
                    "Actual Total Time": 40.0,
                    "Plans": [{
                        "Node Type": "Seq Scan",
                        "Relation Name": "accounts",
                        "Plan Rows": 4800,
                        "Actual Rows": 4800,
                        "Total Cost": 400.0,
                        "Actual Total Time": 35.0,
                    }],
                },
            ],
        },
        "Planning Time": 0.3,
        "Execution Time": 200.3,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "hash_join_build_probe_imbalance" not in names, (
        f"balanced sides (ratio ~1x) should not fire, got {names}"
    )


def test_no_false_positive_hash_join_build_smaller():
    """
    Hash Join where the build side is correctly the smaller relation
    (build=1000, probe=5000, ratio=0.2x — planner made the right choice).
    hash_join_build_probe_imbalance must not fire.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Hash Join",
            "Hash Cond": "(transactions.account_id = accounts.id)",
            "Plan Rows": 5000,
            "Actual Rows": 5000,
            "Total Cost": 8000.0,
            "Actual Total Time": 150.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions",
                    "Plan Rows": 5000,
                    "Actual Rows": 5000,
                    "Total Cost": 500.0,
                    "Actual Total Time": 50.0,
                },
                {
                    "Node Type": "Hash",
                    "Plan Rows": 1000,
                    "Actual Rows": 1000,
                    "Total Cost": 100.0,
                    "Actual Total Time": 10.0,
                    "Plans": [{
                        "Node Type": "Seq Scan",
                        "Relation Name": "accounts",
                        "Plan Rows": 1000,
                        "Actual Rows": 1000,
                        "Total Cost": 80.0,
                        "Actual Total Time": 8.0,
                    }],
                },
            ],
        },
        "Planning Time": 0.3,
        "Execution Time": 150.3,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "hash_join_build_probe_imbalance" not in names, (
        f"build smaller than probe (correct plan) should not fire, got {names}"
    )


def test_planning_time_dominates():
    """
    Plan where planning_time=25ms and execution_time=2.5ms — ratio=10x, well
    above the 5x threshold. planning_time_dominates must fire.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "accounts",
            "Filter": "(id = 42)",
            "Plan Rows": 1,
            "Actual Rows": 1,
            "Total Cost": 2.0,
            "Actual Total Time": 0.5,
        },
        "Planning Time": 25.0,
        "Execution Time": 2.5,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "planning_time_dominates" in names, (
        f"expected planning_time_dominates (ratio=10x), got {names}"
    )


def test_no_false_positive_planning_time_normal():
    """
    Plan where execution_time >> planning_time (ratio=0.002x) — no issue.
    planning_time_dominates must not fire.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "transactions",
            "Plan Rows": 50000,
            "Actual Rows": 50000,
            "Total Cost": 4500.0,
            "Actual Total Time": 490.0,
        },
        "Planning Time": 1.0,
        "Execution Time": 500.0,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "planning_time_dominates" not in names, (
        f"execution >> planning (ratio=0.002x) should not fire, got {names}"
    )


def test_bitmap_heap_lossy_fields_parsed():
    """
    Bitmap Heap Scan with lossy pages: rows_removed_by_recheck, exact_heap_blocks,
    and lossy_heap_blocks must be captured on PlanNode, and recheck_waste_ratio
    must compute rows_removed / (actual_rows + rows_removed).
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Bitmap Heap Scan",
            "Relation Name": "transactions",
            "Recheck Cond": "(amount > 10000)",
            "Rows Removed by Index Recheck": 8700,
            "Exact Heap Blocks": 7,
            "Lossy Heap Blocks": 50,
            "Plan Rows": 1000,
            "Actual Rows": 1300,
            "Total Cost": 2500.0,
            "Actual Total Time": 320.0,
            "Plans": [{
                "Node Type": "Bitmap Index Scan",
                "Index Name": "idx_transactions_amount",
                "Plan Rows": 1000,
                "Actual Rows": 0,
                "Total Cost": 50.0,
                "Actual Total Time": 12.0,
            }],
        },
        "Planning Time": 0.4,
        "Execution Time": 320.4,
    }]
    plan = parse_explain_json(explain_json)
    node = plan.root
    assert node.rows_removed_by_recheck == 8700, (
        f"expected rows_removed_by_recheck=8700, got {node.rows_removed_by_recheck!r}"
    )
    assert node.exact_heap_blocks == 7, (
        f"expected exact_heap_blocks=7, got {node.exact_heap_blocks!r}"
    )
    assert node.lossy_heap_blocks == 50, (
        f"expected lossy_heap_blocks=50, got {node.lossy_heap_blocks!r}"
    )
    expected_ratio = 8700 / (1300 + 8700)
    assert abs(node.recheck_waste_ratio - expected_ratio) < 1e-9, (
        f"expected recheck_waste_ratio={expected_ratio:.4f}, got {node.recheck_waste_ratio:.4f}"
    )


def test_bitmap_heap_lossy_fires_on_high_waste():
    """
    Bitmap Heap Scan: actual_rows=1300, rows_removed_by_recheck=8700 →
    recheck_waste_ratio = 8700/10000 = 0.87 (real-world measured value).
    bitmap_heap_lossy must fire.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Bitmap Heap Scan",
            "Relation Name": "transactions",
            "Recheck Cond": "(amount > 10000)",
            "Rows Removed by Index Recheck": 8700,
            "Exact Heap Blocks": 7,
            "Lossy Heap Blocks": 50,
            "Plan Rows": 1000,
            "Actual Rows": 1300,
            "Total Cost": 2500.0,
            "Actual Total Time": 320.0,
            "Plans": [{
                "Node Type": "Bitmap Index Scan",
                "Index Name": "idx_transactions_amount",
                "Plan Rows": 1000,
                "Actual Rows": 0,
                "Total Cost": 50.0,
                "Actual Total Time": 12.0,
            }],
        },
        "Planning Time": 0.4,
        "Execution Time": 320.4,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "bitmap_heap_lossy" in names, (
        f"expected bitmap_heap_lossy (ratio=0.87), got {names}"
    )


def test_no_false_positive_bitmap_heap_exact_only():
    """
    Bitmap Heap Scan with all-exact blocks and zero rows removed by recheck —
    bitmap fits in work_mem, no lossy pages. bitmap_heap_lossy must not fire.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Bitmap Heap Scan",
            "Relation Name": "transactions",
            "Recheck Cond": "(amount > 10000)",
            "Rows Removed by Index Recheck": 0,
            "Exact Heap Blocks": 55,
            "Lossy Heap Blocks": 0,
            "Plan Rows": 1000,
            "Actual Rows": 1300,
            "Total Cost": 800.0,
            "Actual Total Time": 45.0,
            "Plans": [{
                "Node Type": "Bitmap Index Scan",
                "Index Name": "idx_transactions_amount",
                "Plan Rows": 1000,
                "Actual Rows": 0,
                "Total Cost": 50.0,
                "Actual Total Time": 12.0,
            }],
        },
        "Planning Time": 0.4,
        "Execution Time": 45.4,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "bitmap_heap_lossy" not in names, (
        f"all-exact bitmap (ratio=0.0) should not fire, got {names}"
    )


def test_join_condition_function_wrap_nested_loop():
    """
    Nested Loop where the inner Index Scan's Index Cond has LOWER() on the join
    key — the join condition is on the inner child (children[1].index_condition),
    not on the Nested Loop node itself. join_condition_function_wrap must fire.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Nested Loop",
            "Plan Rows": 500,
            "Actual Rows": 480,
            "Total Cost": 12000.0,
            "Actual Total Time": 280.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions",
                    "Plan Rows": 5000,
                    "Actual Rows": 5000,
                    "Total Cost": 500.0,
                    "Actual Total Time": 50.0,
                    "Actual Loops": 1,
                },
                {
                    "Node Type": "Index Scan",
                    "Relation Name": "merchants",
                    "Index Name": "idx_merchants_name",
                    "Index Cond": "(lower(name) = lower(transactions.merchant_name))",
                    "Plan Rows": 1,
                    "Actual Rows": 1,
                    "Total Cost": 2.0,
                    "Actual Total Time": 0.04,
                    "Actual Loops": 5000,
                },
            ],
        },
        "Planning Time": 0.3,
        "Execution Time": 280.3,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "join_condition_function_wrap" in names, (
        f"expected join_condition_function_wrap (LOWER in inner Index Cond), got {names}"
    )


def test_no_false_positive_nested_loop_plain_index_cond():
    """
    Nested Loop where the inner Index Scan's Index Cond is a plain column
    equality — no function wrap. join_condition_function_wrap must not fire.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Nested Loop",
            "Plan Rows": 500,
            "Actual Rows": 500,
            "Total Cost": 5000.0,
            "Actual Total Time": 80.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions",
                    "Plan Rows": 5000,
                    "Actual Rows": 5000,
                    "Total Cost": 500.0,
                    "Actual Total Time": 50.0,
                    "Actual Loops": 1,
                },
                {
                    "Node Type": "Index Scan",
                    "Relation Name": "accounts",
                    "Index Name": "accounts_pkey",
                    "Index Cond": "(id = transactions.account_id)",
                    "Plan Rows": 1,
                    "Actual Rows": 1,
                    "Total Cost": 0.8,
                    "Actual Total Time": 0.005,
                    "Actual Loops": 5000,
                },
            ],
        },
        "Planning Time": 0.2,
        "Execution Time": 80.2,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "join_condition_function_wrap" not in names, (
        f"plain column Index Cond in NL should not fire, got {names}"
    )


def test_function_scan_bad_estimate():
    """
    Function Scan where the planner guessed 1000 rows (PostgreSQL's flat
    default for set-returning functions) but 100,000 came back — 100x error.
    function_scan_bad_estimate must fire; fix is a ROWS N hint, not ANALYZE.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Function Scan",
            "Function Name": "get_active_accounts",
            "Alias": "f",
            "Plan Rows": 1000,
            "Actual Rows": 100000,
            "Total Cost": 12.5,
            "Actual Total Time": 850.0,
        },
        "Planning Time": 0.3,
        "Execution Time": 850.3,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "function_scan_bad_estimate" in names, (
        f"expected function_scan_bad_estimate (ratio=100x), got {names}"
    )


def test_no_false_positive_function_scan_accurate_estimate():
    """
    Function Scan where the planner's row estimate is close to reality
    (plan=900, actual=950, ratio~1.06x — well below 10x threshold).
    function_scan_bad_estimate must not fire.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Function Scan",
            "Function Name": "get_recent_transactions",
            "Alias": "f",
            "Plan Rows": 900,
            "Actual Rows": 950,
            "Total Cost": 10.0,
            "Actual Total Time": 12.0,
        },
        "Planning Time": 0.2,
        "Execution Time": 12.2,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "function_scan_bad_estimate" not in names, (
        f"ratio~1.06x should not fire function_scan_bad_estimate, got {names}"
    )


def test_parse_parent_relationship():
    """
    SubPlan node carries 'Parent Relationship': 'SubPlan' in EXPLAIN JSON.
    Must be captured on PlanNode.parent_relationship; outer node must be None.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "transactions",
            "Plan Rows": 53425,
            "Actual Rows": 53425,
            "Total Cost": 4600.0,
            "Actual Total Time": 204000.0,
            "Plans": [{
                "Node Type": "Seq Scan",
                "Relation Name": "merchants",
                "Parent Relationship": "SubPlan",
                "Plan Rows": 1,
                "Actual Rows": 1,
                "Total Cost": 15.5,
                "Actual Total Time": 3.5,
                "Actual Loops": 53425,
            }],
        },
        "Planning Time": 0.3,
        "Execution Time": 204000.3,
    }]
    plan = parse_explain_json(explain_json)
    outer = plan.root
    inner = outer.children[0]
    assert outer.parent_relationship is None, (
        f"root node should have parent_relationship=None, got {outer.parent_relationship!r}"
    )
    assert inner.parent_relationship == "SubPlan", (
        f"expected parent_relationship='SubPlan', got {inner.parent_relationship!r}"
    )


def test_subplan_per_row_execution():
    """
    Correlated subquery: merchants Seq Scan runs 53425 times as a SubPlan — one
    execution per outer transactions row, no Nested Loop in the plan.
    subplan_per_row_execution must fire.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "transactions",
            "Plan Rows": 53425,
            "Actual Rows": 53425,
            "Total Cost": 4600.0,
            "Actual Total Time": 204000.0,
            "Plans": [{
                "Node Type": "Seq Scan",
                "Relation Name": "merchants",
                "Parent Relationship": "SubPlan",
                "Plan Rows": 1,
                "Actual Rows": 1,
                "Total Cost": 15.5,
                "Actual Total Time": 3.5,
                "Actual Loops": 53425,
            }],
        },
        "Planning Time": 0.3,
        "Execution Time": 204000.3,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "subplan_per_row_execution" in names, (
        f"expected subplan_per_row_execution (53425 loops), got {names}"
    )


def test_no_false_positive_subplan_single_loop():
    """
    SubPlan executed only once (non-correlated or planner-flattened) — must not fire.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "transactions",
            "Plan Rows": 5000,
            "Actual Rows": 5000,
            "Total Cost": 4600.0,
            "Actual Total Time": 25.0,
            "Plans": [{
                "Node Type": "Seq Scan",
                "Relation Name": "merchants",
                "Parent Relationship": "SubPlan",
                "Plan Rows": 1000,
                "Actual Rows": 1000,
                "Total Cost": 80.0,
                "Actual Total Time": 5.0,
                "Actual Loops": 1,
            }],
        },
        "Planning Time": 0.2,
        "Execution Time": 25.2,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "subplan_per_row_execution" not in names, (
        f"SubPlan with loops=1 should not fire, got {names}"
    )


def test_no_false_positive_nested_loop_inner_high_loops():
    """
    Nested Loop inner Seq Scan with many loops: parent_relationship is 'Inner',
    not 'SubPlan'. subplan_per_row_execution must not fire (repeated_seq_scan_in_loop
    may fire separately — that's expected and correct).
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Nested Loop",
            "Plan Rows": 53425,
            "Actual Rows": 53425,
            "Total Cost": 5000.0,
            "Actual Total Time": 210000.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions",
                    "Parent Relationship": "Outer",
                    "Plan Rows": 53425,
                    "Actual Rows": 53425,
                    "Total Cost": 4600.0,
                    "Actual Total Time": 180000.0,
                    "Actual Loops": 1,
                },
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "merchants",
                    "Parent Relationship": "Inner",
                    "Plan Rows": 1,
                    "Actual Rows": 1,
                    "Total Cost": 15.5,
                    "Actual Total Time": 3.5,
                    "Actual Loops": 53425,
                },
            ],
        },
        "Planning Time": 0.3,
        "Execution Time": 210000.3,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "subplan_per_row_execution" not in names, (
        f"NL inner child (parent_relationship='Inner') must not fire as subplan, got {names}"
    )


def test_parse_hash_agg_fields():
    """Aggregate node with Strategy=Hashed, HashAgg Batches, Disk Usage are parsed correctly."""
    explain_json = [{
        "Plan": {
            "Node Type": "Aggregate",
            "Strategy": "Hashed",
            "HashAgg Batches": 4,
            "Disk Usage": 8192,
            "Plan Rows": 50000,
            "Actual Rows": 45000,
            "Total Cost": 15000.0,
            "Actual Total Time": 2400.0,
        },
        "Planning Time": 0.5,
        "Execution Time": 2400.5,
    }]
    plan = parse_explain_json(explain_json)
    node = plan.root
    assert node.strategy == "Hashed", f"expected strategy='Hashed', got {node.strategy!r}"
    assert node.hash_agg_batches == 4, f"expected hash_agg_batches=4, got {node.hash_agg_batches!r}"
    assert node.disk_usage_kb == 8192, f"expected disk_usage_kb=8192, got {node.disk_usage_kb!r}"


def test_parse_subplans_removed():
    """Append node with Subplans Removed field is parsed into PlanNode.subplans_removed."""
    explain_json = [{
        "Plan": {
            "Node Type": "Append",
            "Subplans Removed": 2,
            "Plan Rows": 20000,
            "Actual Rows": 20000,
            "Total Cost": 4000.0,
            "Actual Total Time": 150.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions_2024_03",
                    "Parent Relationship": "Member",
                    "Plan Rows": 10000,
                    "Actual Rows": 10000,
                    "Total Cost": 2000.0,
                    "Actual Total Time": 75.0,
                },
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions_2024_04",
                    "Parent Relationship": "Member",
                    "Plan Rows": 10000,
                    "Actual Rows": 10000,
                    "Total Cost": 2000.0,
                    "Actual Total Time": 75.0,
                },
            ],
        },
        "Planning Time": 0.3,
        "Execution Time": 150.3,
    }]
    plan = parse_explain_json(explain_json)
    append_node = plan.root
    assert append_node.subplans_removed == 2, (
        f"expected subplans_removed=2, got {append_node.subplans_removed!r}"
    )
    assert append_node.subplans_removed is not None


def test_parse_cte_fields():
    """
    CTE Scan node: cte_name is parsed from 'CTE Name', cte_reference_count is
    populated by the post-parse annotation pass. Two CTE Scan nodes sharing a name
    both get reference_count == 2; a WorkTable Scan sharing the name is excluded.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Nested Loop",
            "Plan Rows": 100,
            "Actual Rows": 100,
            "Total Cost": 600.0,
            "Actual Total Time": 55.0,
            "Plans": [
                {
                    "Node Type": "CTE Scan",
                    "CTE Name": "my_cte",
                    "Parent Relationship": "Outer",
                    "Plan Rows": 10000,
                    "Actual Rows": 10000,
                    "Total Cost": 300.0,
                    "Actual Total Time": 30.0,
                },
                {
                    "Node Type": "CTE Scan",
                    "CTE Name": "my_cte",
                    "Parent Relationship": "Inner",
                    "Plan Rows": 10000,
                    "Actual Rows": 10000,
                    "Total Cost": 300.0,
                    "Actual Total Time": 25.0,
                    "Plans": [{
                        "Node Type": "WorkTable Scan",
                        "CTE Name": "my_cte",
                        "Parent Relationship": "Inner",
                        "Plan Rows": 1,
                        "Actual Rows": 1,
                        "Total Cost": 1.0,
                        "Actual Total Time": 0.1,
                    }],
                },
            ],
        },
        "Planning Time": 0.2,
        "Execution Time": 55.2,
    }]
    plan = parse_explain_json(explain_json)
    nl = plan.root
    cte_a, cte_b = nl.children[0], nl.children[1]
    worktable = cte_b.children[0]

    assert cte_a.cte_name == "my_cte", f"expected cte_name='my_cte', got {cte_a.cte_name!r}"
    assert cte_b.cte_name == "my_cte", f"expected cte_name='my_cte', got {cte_b.cte_name!r}"
    assert worktable.cte_name == "my_cte", f"expected worktable cte_name='my_cte', got {worktable.cte_name!r}"
    assert cte_a.cte_reference_count == 2, (
        f"two CTE Scans with same name → each should have cte_reference_count=2, got {cte_a.cte_reference_count}"
    )
    assert cte_b.cte_reference_count == 2, (
        f"two CTE Scans with same name → each should have cte_reference_count=2, got {cte_b.cte_reference_count}"
    )
    assert worktable.cte_reference_count == 0, (
        f"WorkTable Scan must not be counted — expected cte_reference_count=0, got {worktable.cte_reference_count}"
    )


def test_hash_aggregate_disk_spill():
    """
    Aggregate (Strategy=Hashed) with Disk Usage=8192 KB — hash table exceeded
    work_mem and spilled to disk. hash_aggregate_disk_spill must fire.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Aggregate",
            "Strategy": "Hashed",
            "HashAgg Batches": 4,
            "Disk Usage": 8192,
            "Plan Rows": 50000,
            "Actual Rows": 45000,
            "Total Cost": 15000.0,
            "Actual Total Time": 2400.0,
            "Plans": [{
                "Node Type": "Seq Scan",
                "Relation Name": "transactions",
                "Plan Rows": 200000,
                "Actual Rows": 200000,
                "Total Cost": 4500.0,
                "Actual Total Time": 300.0,
            }],
        },
        "Planning Time": 0.5,
        "Execution Time": 2400.5,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "hash_aggregate_disk_spill" in names, (
        f"expected hash_aggregate_disk_spill (Disk Usage=8192 KB), got {names}"
    )


def test_no_false_positive_hash_agg_no_spill():
    """
    Aggregate (Strategy=Hashed) with Disk Usage=0 — hash table fit entirely in
    work_mem. hash_aggregate_disk_spill must not fire.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Aggregate",
            "Strategy": "Hashed",
            "HashAgg Batches": 1,
            "Disk Usage": 0,
            "Plan Rows": 5000,
            "Actual Rows": 4800,
            "Total Cost": 2000.0,
            "Actual Total Time": 80.0,
            "Plans": [{
                "Node Type": "Seq Scan",
                "Relation Name": "transactions",
                "Plan Rows": 5000,
                "Actual Rows": 4800,
                "Total Cost": 500.0,
                "Actual Total Time": 20.0,
            }],
        },
        "Planning Time": 0.3,
        "Execution Time": 80.3,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "hash_aggregate_disk_spill" not in names, (
        f"in-memory HashAgg (Disk Usage=0) should not fire, got {names}"
    )


def test_bitmap_or_missing_index_branch():
    """BitmapOr → Seq Scan on txn_type with NO index on txn_type — genuine gap, must fire."""
    schema = {
        "transactions": TableSchema(
            table_name="transactions",
            indexes=[
                IndexInfo(
                    name="idx_transactions_status",
                    definition="CREATE INDEX idx_transactions_status ON public.transactions USING btree (status)",
                ),
            ],
        )
    }
    explain_json = [{
        "Plan": {
            "Node Type": "Bitmap Heap Scan",
            "Relation Name": "transactions",
            "Plan Rows": 15000,
            "Actual Rows": 15000,
            "Total Cost": 6000.0,
            "Actual Total Time": 450.0,
            "Plans": [{
                "Node Type": "BitmapOr",
                "Plan Rows": 15000,
                "Actual Rows": 15000,
                "Total Cost": 5000.0,
                "Actual Total Time": 380.0,
                "Plans": [
                    {
                        "Node Type": "Bitmap Index Scan",
                        "Index Name": "idx_transactions_status",
                        "Plan Rows": 10000,
                        "Actual Rows": 10000,
                        "Total Cost": 200.0,
                        "Actual Total Time": 30.0,
                    },
                    {
                        "Node Type": "Seq Scan",
                        "Relation Name": "transactions",
                        "Filter": "(txn_type = 'REFUND')",
                        "Plan Rows": 5000,
                        "Actual Rows": 5000,
                        "Total Cost": 4800.0,
                        "Actual Total Time": 350.0,
                    },
                ],
            }],
        },
        "Planning Time": 0.4,
        "Execution Time": 450.4,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK, schema_context=schema)
    names = {m.skill_name for m in result.matches}
    assert "bitmap_or_missing_index_branch" in names, (
        f"expected bitmap_or_missing_index_branch (Seq Scan child of BitmapOr, no index on txn_type), got {names}"
    )


def test_no_false_positive_bitmap_or_seq_scan_indexed_column():
    """
    v1 false positive: BitmapOr → Seq Scan on 'status' but status HAS an index.
    Planner chose Seq Scan for selectivity reasons (e.g. ~92% of rows match).
    v2 must NOT fire — an index exists, this is a planner decision, not a gap.
    """
    schema = {
        "transactions": TableSchema(
            table_name="transactions",
            indexes=[
                IndexInfo(
                    name="idx_transactions_status",
                    definition="CREATE INDEX idx_transactions_status ON public.transactions USING btree (status)",
                ),
                IndexInfo(
                    name="idx_transactions_merchant",
                    definition="CREATE INDEX idx_transactions_merchant ON public.transactions USING btree (merchant)",
                ),
            ],
        )
    }
    explain_json = [{
        "Plan": {
            "Node Type": "Bitmap Heap Scan",
            "Relation Name": "transactions",
            "Plan Rows": 95000,
            "Actual Rows": 95000,
            "Total Cost": 9000.0,
            "Actual Total Time": 680.0,
            "Plans": [{
                "Node Type": "BitmapOr",
                "Plan Rows": 95000,
                "Actual Rows": 95000,
                "Total Cost": 8500.0,
                "Actual Total Time": 600.0,
                "Plans": [
                    {
                        "Node Type": "Bitmap Index Scan",
                        "Index Name": "idx_transactions_merchant",
                        "Plan Rows": 5000,
                        "Actual Rows": 5000,
                        "Total Cost": 200.0,
                        "Actual Total Time": 30.0,
                    },
                    {
                        "Node Type": "Seq Scan",
                        "Relation Name": "transactions",
                        "Filter": "(status = 'COMPLETED')",
                        "Plan Rows": 90000,
                        "Actual Rows": 90000,
                        "Total Cost": 8300.0,
                        "Actual Total Time": 570.0,
                    },
                ],
            }],
        },
        "Planning Time": 0.4,
        "Execution Time": 680.4,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK, schema_context=schema)
    names = {m.skill_name for m in result.matches}
    assert "bitmap_or_missing_index_branch" not in names, (
        "index exists on 'status' — Seq Scan is a planner selectivity choice, "
        f"not a missing-index gap; skill must not fire, got {names}"
    )


def test_no_false_positive_bitmap_or_no_schema_context():
    """
    BitmapOr → Seq Scan shape matches but schema_context is None — skill must
    abstain (SCHEMA_UNAVAILABLE), not fire. Without schema we can't distinguish
    a genuine missing-index gap from a valid selectivity-driven Seq Scan.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Bitmap Heap Scan",
            "Relation Name": "transactions",
            "Plan Rows": 15000,
            "Actual Rows": 15000,
            "Total Cost": 6000.0,
            "Actual Total Time": 450.0,
            "Plans": [{
                "Node Type": "BitmapOr",
                "Plan Rows": 15000,
                "Actual Rows": 15000,
                "Total Cost": 5000.0,
                "Actual Total Time": 380.0,
                "Plans": [
                    {
                        "Node Type": "Bitmap Index Scan",
                        "Index Name": "idx_transactions_status",
                        "Plan Rows": 10000,
                        "Actual Rows": 10000,
                        "Total Cost": 200.0,
                        "Actual Total Time": 30.0,
                    },
                    {
                        "Node Type": "Seq Scan",
                        "Relation Name": "transactions",
                        "Filter": "(txn_type = 'REFUND')",
                        "Plan Rows": 5000,
                        "Actual Rows": 5000,
                        "Total Cost": 4800.0,
                        "Actual Total Time": 350.0,
                    },
                ],
            }],
        },
        "Planning Time": 0.4,
        "Execution Time": 450.4,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK, schema_context=None)
    names = {m.skill_name for m in result.matches}
    assert "bitmap_or_missing_index_branch" not in names, (
        f"skill must abstain when schema_context is None, got {names}"
    )


def test_no_false_positive_bitmap_or_all_index_scans():
    """BitmapOr with all Bitmap Index Scan children — every OR-branch has an index, must not fire."""
    explain_json = [{
        "Plan": {
            "Node Type": "Bitmap Heap Scan",
            "Relation Name": "transactions",
            "Plan Rows": 15000,
            "Actual Rows": 15000,
            "Total Cost": 3000.0,
            "Actual Total Time": 120.0,
            "Plans": [{
                "Node Type": "BitmapOr",
                "Plan Rows": 15000,
                "Actual Rows": 15000,
                "Total Cost": 2500.0,
                "Actual Total Time": 80.0,
                "Plans": [
                    {
                        "Node Type": "Bitmap Index Scan",
                        "Index Name": "idx_transactions_status",
                        "Plan Rows": 10000,
                        "Actual Rows": 10000,
                        "Total Cost": 200.0,
                        "Actual Total Time": 30.0,
                    },
                    {
                        "Node Type": "Bitmap Index Scan",
                        "Index Name": "idx_transactions_txn_type",
                        "Plan Rows": 5000,
                        "Actual Rows": 5000,
                        "Total Cost": 100.0,
                        "Actual Total Time": 20.0,
                    },
                ],
            }],
        },
        "Planning Time": 0.3,
        "Execution Time": 120.3,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "bitmap_or_missing_index_branch" not in names, (
        f"all BitmapOr children are index scans, should not fire, got {names}"
    )


def test_merge_join_child_sort_spill_outer():
    """Merge Join where the outer Sort child spilled to disk — must fire."""
    explain_json = [{
        "Plan": {
            "Node Type": "Merge Join",
            "Merge Cond": "(t.account_id = m.id)",
            "Plan Rows": 500000,
            "Actual Rows": 500000,
            "Total Cost": 20000.0,
            "Actual Total Time": 3500.0,
            "Plans": [
                {
                    "Node Type": "Sort",
                    "Sort Key": ["t.account_id"],
                    "Sort Method": "external merge",
                    "Sort Space Used": 102400,
                    "Sort Space Type": "Disk",
                    "Plan Rows": 500000,
                    "Actual Rows": 500000,
                    "Total Cost": 15000.0,
                    "Actual Total Time": 2000.0,
                    "Plans": [{
                        "Node Type": "Seq Scan",
                        "Relation Name": "transactions",
                        "Plan Rows": 500000,
                        "Actual Rows": 500000,
                        "Total Cost": 8000.0,
                        "Actual Total Time": 800.0,
                    }],
                },
                {
                    "Node Type": "Sort",
                    "Sort Key": ["m.id"],
                    "Sort Method": "quicksort",
                    "Sort Space Used": 1024,
                    "Sort Space Type": "Memory",
                    "Plan Rows": 10000,
                    "Actual Rows": 10000,
                    "Total Cost": 2000.0,
                    "Actual Total Time": 100.0,
                    "Plans": [{
                        "Node Type": "Seq Scan",
                        "Relation Name": "merchants",
                        "Plan Rows": 10000,
                        "Actual Rows": 10000,
                        "Total Cost": 500.0,
                        "Actual Total Time": 50.0,
                    }],
                },
            ],
        },
        "Planning Time": 0.8,
        "Execution Time": 3500.8,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "merge_join_child_sort_spill" in names, (
        f"expected merge_join_child_sort_spill (outer Sort spilled), got {names}"
    )


def test_merge_join_child_sort_spill_inner():
    """Merge Join where only the INNER Sort spilled — proves any_child checks all children, not just first."""
    explain_json = [{
        "Plan": {
            "Node Type": "Merge Join",
            "Merge Cond": "(t.account_id = m.id)",
            "Plan Rows": 500000,
            "Actual Rows": 500000,
            "Total Cost": 20000.0,
            "Actual Total Time": 3500.0,
            "Plans": [
                {
                    "Node Type": "Sort",
                    "Sort Key": ["t.account_id"],
                    "Sort Method": "quicksort",
                    "Sort Space Used": 4096,
                    "Sort Space Type": "Memory",
                    "Plan Rows": 500000,
                    "Actual Rows": 500000,
                    "Total Cost": 15000.0,
                    "Actual Total Time": 800.0,
                    "Plans": [{
                        "Node Type": "Seq Scan",
                        "Relation Name": "transactions",
                        "Plan Rows": 500000,
                        "Actual Rows": 500000,
                        "Total Cost": 8000.0,
                        "Actual Total Time": 600.0,
                    }],
                },
                {
                    "Node Type": "Sort",
                    "Sort Key": ["m.id"],
                    "Sort Method": "external merge",
                    "Sort Space Used": 204800,
                    "Sort Space Type": "Disk",
                    "Plan Rows": 800000,
                    "Actual Rows": 800000,
                    "Total Cost": 18000.0,
                    "Actual Total Time": 2800.0,
                    "Plans": [{
                        "Node Type": "Seq Scan",
                        "Relation Name": "merchants",
                        "Plan Rows": 800000,
                        "Actual Rows": 800000,
                        "Total Cost": 9000.0,
                        "Actual Total Time": 900.0,
                    }],
                },
            ],
        },
        "Planning Time": 0.9,
        "Execution Time": 3500.9,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "merge_join_child_sort_spill" in names, (
        f"expected merge_join_child_sort_spill (inner Sort spilled), got {names}"
    )


def test_no_false_positive_merge_join_no_spill():
    """Merge Join where neither Sort child spilled — both quicksort, must not fire."""
    explain_json = [{
        "Plan": {
            "Node Type": "Merge Join",
            "Merge Cond": "(t.account_id = m.id)",
            "Plan Rows": 50000,
            "Actual Rows": 50000,
            "Total Cost": 5000.0,
            "Actual Total Time": 300.0,
            "Plans": [
                {
                    "Node Type": "Sort",
                    "Sort Key": ["t.account_id"],
                    "Sort Method": "quicksort",
                    "Sort Space Used": 2048,
                    "Sort Space Type": "Memory",
                    "Plan Rows": 50000,
                    "Actual Rows": 50000,
                    "Total Cost": 3000.0,
                    "Actual Total Time": 200.0,
                    "Plans": [{
                        "Node Type": "Seq Scan",
                        "Relation Name": "transactions",
                        "Plan Rows": 50000,
                        "Actual Rows": 50000,
                        "Total Cost": 1500.0,
                        "Actual Total Time": 100.0,
                    }],
                },
                {
                    "Node Type": "Sort",
                    "Sort Key": ["m.id"],
                    "Sort Method": "quicksort",
                    "Sort Space Used": 512,
                    "Sort Space Type": "Memory",
                    "Plan Rows": 5000,
                    "Actual Rows": 5000,
                    "Total Cost": 800.0,
                    "Actual Total Time": 50.0,
                    "Plans": [{
                        "Node Type": "Seq Scan",
                        "Relation Name": "merchants",
                        "Plan Rows": 5000,
                        "Actual Rows": 5000,
                        "Total Cost": 300.0,
                        "Actual Total Time": 25.0,
                    }],
                },
            ],
        },
        "Planning Time": 0.5,
        "Execution Time": 300.5,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "merge_join_child_sort_spill" not in names, (
        f"neither Sort spilled, should not fire merge_join_child_sort_spill, got {names}"
    )


def test_initplan_expensive():
    """InitPlan Aggregate consuming 40% of execution time — must fire."""
    explain_json = [{
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "orders",
            "Plan Rows": 100000,
            "Actual Rows": 100000,
            "Total Cost": 5000.0,
            "Actual Total Time": 600.0,
            "Plans": [{
                "Node Type": "Aggregate",
                "Parent Relationship": "InitPlan",
                "Strategy": "Plain",
                "Plan Rows": 1,
                "Actual Rows": 1,
                "Total Cost": 3000.0,
                "Actual Total Time": 400.0,
                "Actual Loops": 1,
                "Plans": [{
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions",
                    "Plan Rows": 2000000,
                    "Actual Rows": 2000000,
                    "Total Cost": 2900.0,
                    "Actual Total Time": 380.0,
                }],
            }],
        },
        "Planning Time": 1.5,
        "Execution Time": 1000.0,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "initplan_expensive" in names, (
        f"expected initplan_expensive (InitPlan ratio=0.40), got {names}"
    )


def test_no_false_positive_initplan_cheap():
    """InitPlan consuming only 5% of execution time — below 0.3 threshold, must not fire."""
    explain_json = [{
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "orders",
            "Plan Rows": 100000,
            "Actual Rows": 100000,
            "Total Cost": 5000.0,
            "Actual Total Time": 950.0,
            "Plans": [{
                "Node Type": "Aggregate",
                "Parent Relationship": "InitPlan",
                "Strategy": "Plain",
                "Plan Rows": 1,
                "Actual Rows": 1,
                "Total Cost": 200.0,
                "Actual Total Time": 50.0,
                "Actual Loops": 1,
                "Plans": [{
                    "Node Type": "Index Only Scan",
                    "Relation Name": "transactions",
                    "Index Name": "idx_transactions_amount",
                    "Plan Rows": 1,
                    "Actual Rows": 1,
                    "Total Cost": 100.0,
                    "Actual Total Time": 40.0,
                    "Heap Fetches": 0,
                }],
            }],
        },
        "Planning Time": 0.8,
        "Execution Time": 1000.0,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "initplan_expensive" not in names, (
        f"cheap InitPlan (ratio=0.05) should not fire initplan_expensive, got {names}"
    )


def test_no_false_positive_subplan_not_initplan():
    """SubPlan (correlated) with high actual_total_time — wrong parent relationship, must not fire."""
    explain_json = [{
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "orders",
            "Plan Rows": 100000,
            "Actual Rows": 100000,
            "Total Cost": 5000.0,
            "Actual Total Time": 600.0,
            "Plans": [{
                "Node Type": "Aggregate",
                "Parent Relationship": "SubPlan",
                "Strategy": "Plain",
                "Plan Rows": 1,
                "Actual Rows": 1,
                "Total Cost": 3000.0,
                "Actual Total Time": 400.0,
                "Actual Loops": 100000,
                "Plans": [{
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions",
                    "Plan Rows": 1,
                    "Actual Rows": 1,
                    "Total Cost": 2900.0,
                    "Actual Total Time": 3.8,
                }],
            }],
        },
        "Planning Time": 1.5,
        "Execution Time": 1000.0,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "initplan_expensive" not in names, (
        f"SubPlan parent relationship should not fire initplan_expensive, got {names}"
    )


def test_initplan_aggregate_expensive():
    """
    Three InitPlan Aggregates each consuming 150ms of a 600ms total (each ratio=0.25,
    below the 0.3 per-node threshold). initplan_expensive must NOT fire on any of them.
    Aggregate sum=450ms=75% of total — initplan_aggregate_expensive MUST fire.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "orders",
            "Plan Rows": 200000,
            "Actual Rows": 200000,
            "Total Cost": 9000.0,
            "Actual Total Time": 150.0,
            "Plans": [
                {
                    "Node Type": "Aggregate",
                    "Parent Relationship": "InitPlan",
                    "Strategy": "Plain",
                    "Plan Rows": 1,
                    "Actual Rows": 1,
                    "Total Cost": 2800.0,
                    "Actual Total Time": 150.0,
                    "Actual Loops": 1,
                    "Plans": [{
                        "Node Type": "Seq Scan",
                        "Relation Name": "transactions",
                        "Plan Rows": 1000000,
                        "Actual Rows": 1000000,
                        "Total Cost": 2700.0,
                        "Actual Total Time": 140.0,
                    }],
                },
                {
                    "Node Type": "Aggregate",
                    "Parent Relationship": "InitPlan",
                    "Strategy": "Plain",
                    "Plan Rows": 1,
                    "Actual Rows": 1,
                    "Total Cost": 2800.0,
                    "Actual Total Time": 150.0,
                    "Actual Loops": 1,
                    "Plans": [{
                        "Node Type": "Seq Scan",
                        "Relation Name": "accounts",
                        "Plan Rows": 1000000,
                        "Actual Rows": 1000000,
                        "Total Cost": 2700.0,
                        "Actual Total Time": 140.0,
                    }],
                },
                {
                    "Node Type": "Aggregate",
                    "Parent Relationship": "InitPlan",
                    "Strategy": "Plain",
                    "Plan Rows": 1,
                    "Actual Rows": 1,
                    "Total Cost": 2800.0,
                    "Actual Total Time": 150.0,
                    "Actual Loops": 1,
                    "Plans": [{
                        "Node Type": "Seq Scan",
                        "Relation Name": "merchants",
                        "Plan Rows": 1000000,
                        "Actual Rows": 1000000,
                        "Total Cost": 2700.0,
                        "Actual Total Time": 140.0,
                    }],
                },
            ],
        },
        "Planning Time": 0.5,
        "Execution Time": 600.0,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "initplan_aggregate_expensive" in names, (
        f"expected initplan_aggregate_expensive (3×150ms=75% of 600ms), got {names}"
    )
    assert "initplan_expensive" not in names, (
        f"initplan_expensive should not fire when each individual ratio=0.25, got {names}"
    )


def test_no_false_positive_initplan_aggregate_cheap():
    """
    Three InitPlan Aggregates each consuming 50ms of a 600ms total.
    Aggregate sum=150ms=25% of total — below 0.3 threshold, must not fire.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "orders",
            "Plan Rows": 200000,
            "Actual Rows": 200000,
            "Total Cost": 9000.0,
            "Actual Total Time": 450.0,
            "Plans": [
                {
                    "Node Type": "Aggregate",
                    "Parent Relationship": "InitPlan",
                    "Strategy": "Plain",
                    "Plan Rows": 1,
                    "Actual Rows": 1,
                    "Total Cost": 300.0,
                    "Actual Total Time": 50.0,
                    "Actual Loops": 1,
                    "Plans": [{
                        "Node Type": "Index Only Scan",
                        "Relation Name": "transactions",
                        "Index Name": "idx_transactions_amount",
                        "Plan Rows": 1,
                        "Actual Rows": 1,
                        "Total Cost": 200.0,
                        "Actual Total Time": 40.0,
                        "Heap Fetches": 0,
                    }],
                },
                {
                    "Node Type": "Aggregate",
                    "Parent Relationship": "InitPlan",
                    "Strategy": "Plain",
                    "Plan Rows": 1,
                    "Actual Rows": 1,
                    "Total Cost": 300.0,
                    "Actual Total Time": 50.0,
                    "Actual Loops": 1,
                    "Plans": [{
                        "Node Type": "Index Only Scan",
                        "Relation Name": "accounts",
                        "Index Name": "idx_accounts_balance",
                        "Plan Rows": 1,
                        "Actual Rows": 1,
                        "Total Cost": 200.0,
                        "Actual Total Time": 40.0,
                        "Heap Fetches": 0,
                    }],
                },
                {
                    "Node Type": "Aggregate",
                    "Parent Relationship": "InitPlan",
                    "Strategy": "Plain",
                    "Plan Rows": 1,
                    "Actual Rows": 1,
                    "Total Cost": 300.0,
                    "Actual Total Time": 50.0,
                    "Actual Loops": 1,
                    "Plans": [{
                        "Node Type": "Index Only Scan",
                        "Relation Name": "merchants",
                        "Index Name": "idx_merchants_id",
                        "Plan Rows": 1,
                        "Actual Rows": 1,
                        "Total Cost": 200.0,
                        "Actual Total Time": 40.0,
                        "Heap Fetches": 0,
                    }],
                },
            ],
        },
        "Planning Time": 0.5,
        "Execution Time": 600.0,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "initplan_aggregate_expensive" not in names, (
        f"aggregate sum=25% should not fire initplan_aggregate_expensive, got {names}"
    )


def test_no_false_positive_initplan_aggregate_single_node():
    """
    Single InitPlan at 200ms of a 400ms total (ratio=0.5, above per-node threshold).
    initplan_expensive must fire. initplan_aggregate_expensive must NOT fire —
    min_count=2 not satisfied.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "orders",
            "Plan Rows": 50000,
            "Actual Rows": 50000,
            "Total Cost": 5000.0,
            "Actual Total Time": 200.0,
            "Plans": [{
                "Node Type": "Aggregate",
                "Parent Relationship": "InitPlan",
                "Strategy": "Plain",
                "Plan Rows": 1,
                "Actual Rows": 1,
                "Total Cost": 3000.0,
                "Actual Total Time": 200.0,
                "Actual Loops": 1,
                "Plans": [{
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions",
                    "Plan Rows": 2000000,
                    "Actual Rows": 2000000,
                    "Total Cost": 2900.0,
                    "Actual Total Time": 180.0,
                }],
            }],
        },
        "Planning Time": 0.5,
        "Execution Time": 400.0,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "initplan_expensive" in names, (
        f"single InitPlan at 50% of total must fire initplan_expensive, got {names}"
    )
    assert "initplan_aggregate_expensive" not in names, (
        f"single InitPlan (count=1 < min_count=2) must not fire aggregate skill, got {names}"
    )


def test_sort_expression_no_index():
    """Sort node with LOWER() in sort key — function wrap prevents index use. Must fire."""
    explain_json = [{
        "Plan": {
            "Node Type": "Sort",
            "Sort Key": ["lower(name)"],
            "Sort Method": "quicksort",
            "Sort Space Used": 512,
            "Sort Space Type": "Memory",
            "Plan Rows": 50000,
            "Actual Rows": 50000,
            "Total Cost": 3000.0,
            "Actual Total Time": 250.0,
            "Plans": [{
                "Node Type": "Seq Scan",
                "Relation Name": "merchants",
                "Plan Rows": 50000,
                "Actual Rows": 50000,
                "Total Cost": 800.0,
                "Actual Total Time": 100.0,
            }],
        },
        "Planning Time": 0.4,
        "Execution Time": 250.4,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "sort_expression_no_index" in names, (
        f"expected sort_expression_no_index (LOWER in sort key), got {names}"
    )


def test_unique_without_index():
    """Unique → Sort with 500k rows in Sort child — large pre-dedup volume. Must fire."""
    explain_json = [{
        "Plan": {
            "Node Type": "Unique",
            "Plan Rows": 45000,
            "Actual Rows": 45000,
            "Total Cost": 10000.0,
            "Actual Total Time": 800.0,
            "Plans": [{
                "Node Type": "Sort",
                "Sort Key": ["account_id"],
                "Sort Method": "quicksort",
                "Sort Space Used": 8192,
                "Sort Space Type": "Memory",
                "Plan Rows": 500000,
                "Actual Rows": 500000,
                "Total Cost": 9500.0,
                "Actual Total Time": 750.0,
                "Plans": [{
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions",
                    "Plan Rows": 500000,
                    "Actual Rows": 500000,
                    "Total Cost": 8000.0,
                    "Actual Total Time": 600.0,
                }],
            }],
        },
        "Planning Time": 0.5,
        "Execution Time": 800.5,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "unique_without_index" in names, (
        f"expected unique_without_index (Sort child 500k rows), got {names}"
    )


def test_no_false_positive_unique_small_input():
    """Unique → Sort with only 12 rows in Sort child — below 1000 threshold, must not fire."""
    explain_json = [{
        "Plan": {
            "Node Type": "Unique",
            "Plan Rows": 10,
            "Actual Rows": 10,
            "Total Cost": 5.0,
            "Actual Total Time": 0.1,
            "Plans": [{
                "Node Type": "Sort",
                "Sort Key": ["status"],
                "Sort Method": "quicksort",
                "Sort Space Used": 2,
                "Sort Space Type": "Memory",
                "Plan Rows": 12,
                "Actual Rows": 12,
                "Total Cost": 4.0,
                "Actual Total Time": 0.08,
                "Plans": [{
                    "Node Type": "Seq Scan",
                    "Relation Name": "txn_statuses",
                    "Plan Rows": 12,
                    "Actual Rows": 12,
                    "Total Cost": 1.0,
                    "Actual Total Time": 0.02,
                }],
            }],
        },
        "Planning Time": 0.1,
        "Execution Time": 0.2,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "unique_without_index" not in names, (
        f"Sort child with only 12 rows should not fire unique_without_index, got {names}"
    )


def test_no_false_positive_sort_plain_column():
    """Sort node with plain column sort key — no function wrap, must not fire."""
    explain_json = [{
        "Plan": {
            "Node Type": "Sort",
            "Sort Key": ["account_id"],
            "Sort Method": "quicksort",
            "Sort Space Used": 256,
            "Sort Space Type": "Memory",
            "Plan Rows": 5000,
            "Actual Rows": 5000,
            "Total Cost": 2000.0,
            "Actual Total Time": 120.0,
            "Plans": [{
                "Node Type": "Seq Scan",
                "Relation Name": "transactions",
                "Plan Rows": 5000,
                "Actual Rows": 5000,
                "Total Cost": 1000.0,
                "Actual Total Time": 60.0,
            }],
        },
        "Planning Time": 0.2,
        "Execution Time": 120.2,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "sort_expression_no_index" not in names, (
        f"plain column sort key should not fire sort_expression_no_index, got {names}"
    )


def test_schema_context_threads_through_match_skills():
    """
    Integration test: schema_context flows from match_skills() through
    matches_node() into _evaluate_rules() and reaches the predicate with
    real TableSchema data — not just that the parameter compiles.

    Uses a skill with requires_schema_context: true as the observable:
    - With schema_context containing a real TableSchema, the skill fires.
    - With schema_context=None, the skill abstains.

    This is the contract that makes the cli.py wiring meaningful: what
    introspect_query_tables() returns must reach the predicate unchanged.
    """
    schema_skill = Skill(
        name="test_schema_chain",
        description="",
        detects={"node_type": "Seq Scan", "requires_schema_context": True},
        severity="medium",
        explanation="",
        fix_template="",
        covers_node_types=["Seq Scan"],
    )
    explain_json = [{
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "transactions",
            "Plan Rows": 50000,
            "Actual Rows": 50000,
            "Total Cost": 4000.0,
            "Actual Total Time": 320.0,
        },
        "Planning Time": 0.3,
        "Execution Time": 320.3,
    }]
    plan = parse_explain_json(explain_json)

    # A real TableSchema object — the same type introspect_query_tables() returns.
    # Populated with actual columns and an index, not just an empty placeholder.
    real_schema: dict[str, TableSchema] = {
        "transactions": TableSchema(
            table_name="transactions",
            columns=[
                ColumnInfo(name="id", data_type="integer"),
                ColumnInfo(name="account_id", data_type="integer"),
                ColumnInfo(name="amount", data_type="numeric"),
            ],
            indexes=[
                IndexInfo(
                    name="idx_transactions_account_id",
                    definition="CREATE INDEX idx_transactions_account_id ON public.transactions USING btree (account_id)",
                ),
            ],
            row_estimate=200000,
        )
    }

    # With real schema_context: gate passes, skill fires on the Seq Scan node.
    result_with = match_skills(
        plan, [schema_skill], ledger_status=LedgerStatus.OK, schema_context=real_schema
    )
    assert any(m.skill_name == "test_schema_chain" for m in result_with.matches), (
        "schema_context with real TableSchema data must reach the predicate via "
        "match_skills → matches_node → _evaluate_rules"
    )

    # With schema_context=None: gate abstains, skill does not fire.
    result_without = match_skills(
        plan, [schema_skill], ledger_status=LedgerStatus.OK, schema_context=None
    )
    assert not any(m.skill_name == "test_schema_chain" for m in result_without.matches), (
        "schema_context=None must cause the skill to abstain, even when match_skills "
        "receives a plan that would otherwise match"
    )


def test_schema_context_abstain_when_none():
    """
    A skill declaring requires_schema_context: true must not fire when
    schema_context is absent — even on a fixture that satisfies all other
    detection predicates (node_type matches, no other guards). The
    abstain-when-None contract must be explicit and tested, not assumed.
    This is the property that makes schema-dependent skills safe to ship:
    offline runs against synthetic fixtures silently skip rather than
    false-positive-fire.
    """
    schema_skill = Skill(
        name="test_schema_gate",
        description="",
        detects={"node_type": "Seq Scan", "requires_schema_context": True},
        severity="medium",
        explanation="",
        fix_template="",
        covers_node_types=["Seq Scan"],
    )
    explain_json = [{
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "transactions",
            "Plan Rows": 50000,
            "Actual Rows": 50000,
            "Total Cost": 4000.0,
            "Actual Total Time": 320.0,
        },
        "Planning Time": 0.3,
        "Execution Time": 320.3,
    }]
    plan = parse_explain_json(explain_json)
    node = plan.root

    assert not schema_skill.matches_node(node, schema_context=None), (
        "requires_schema_context skill must abstain (return False) when "
        "schema_context=None, even on a node that satisfies node_type"
    )


def test_schema_context_gate_passes_when_provided():
    """
    Same skill as above: when schema_context IS provided (even an empty dict —
    no tables introspected yet), the gate passes and the skill evaluates its
    remaining predicates normally. Proves the gate is purely a None-check,
    not a 'table is present in the dict' check — that belongs to individual
    schema-specific predicates added later.
    """
    schema_skill = Skill(
        name="test_schema_gate",
        description="",
        detects={"node_type": "Seq Scan", "requires_schema_context": True},
        severity="medium",
        explanation="",
        fix_template="",
        covers_node_types=["Seq Scan"],
    )
    explain_json = [{
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "transactions",
            "Plan Rows": 50000,
            "Actual Rows": 50000,
            "Total Cost": 4000.0,
            "Actual Total Time": 320.0,
        },
        "Planning Time": 0.3,
        "Execution Time": 320.3,
    }]
    plan = parse_explain_json(explain_json)
    node = plan.root

    assert schema_skill.matches_node(node, schema_context={}), (
        "requires_schema_context skill must fire when schema_context is provided "
        "(empty dict is sufficient — the gate is a None-check, not a lookup)"
    )


def test_modify_table_seq_scan():
    """ModifyTable (UPDATE) with a direct Seq Scan child scanning many rows — must fire."""
    explain_json = [{
        "Plan": {
            "Node Type": "ModifyTable",
            "Operation": "Update",
            "Relation Name": "transactions",
            "Plan Rows": 50000,
            "Actual Rows": 50000,
            "Total Cost": 9000.0,
            "Actual Total Time": 820.0,
            "Plans": [{
                "Node Type": "Seq Scan",
                "Relation Name": "transactions",
                "Filter": "(status = 'PENDING')",
                "Plan Rows": 50000,
                "Actual Rows": 50000,
                "Total Cost": 8500.0,
                "Actual Total Time": 780.0,
            }],
        },
        "Planning Time": 0.5,
        "Execution Time": 820.5,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "modify_table_seq_scan" in names, (
        f"expected modify_table_seq_scan for UPDATE with large Seq Scan child, got {names}"
    )


def test_modify_table_seq_scan_deep():
    """
    ModifyTable → Subquery Scan → Seq Scan (depth 2). any_descendant must reach
    through the interposed Subquery Scan and still fire.
    """
    explain_json = [{
        "Plan": {
            "Node Type": "ModifyTable",
            "Operation": "Update",
            "Relation Name": "transactions",
            "Plan Rows": 50000,
            "Actual Rows": 50000,
            "Total Cost": 9500.0,
            "Actual Total Time": 850.0,
            "Plans": [{
                "Node Type": "Subquery Scan",
                "Alias": "t",
                "Plan Rows": 50000,
                "Actual Rows": 50000,
                "Total Cost": 9000.0,
                "Actual Total Time": 820.0,
                "Plans": [{
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions",
                    "Filter": "(status = 'PENDING')",
                    "Plan Rows": 50000,
                    "Actual Rows": 50000,
                    "Total Cost": 8500.0,
                    "Actual Total Time": 780.0,
                }],
            }],
        },
        "Planning Time": 0.5,
        "Execution Time": 850.5,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "modify_table_seq_scan" in names, (
        f"expected modify_table_seq_scan for UPDATE with Seq Scan at depth 2, got {names}"
    )


def test_no_false_positive_modify_table_index_scan():
    """ModifyTable with an Index Scan descendant — plan uses an index, must NOT fire."""
    explain_json = [{
        "Plan": {
            "Node Type": "ModifyTable",
            "Operation": "Update",
            "Relation Name": "transactions",
            "Plan Rows": 1,
            "Actual Rows": 1,
            "Total Cost": 10.5,
            "Actual Total Time": 0.8,
            "Plans": [{
                "Node Type": "Index Scan",
                "Relation Name": "transactions",
                "Index Name": "idx_transactions_id",
                "Plan Rows": 1,
                "Actual Rows": 1,
                "Total Cost": 8.3,
                "Actual Total Time": 0.5,
            }],
        },
        "Planning Time": 0.3,
        "Execution Time": 1.1,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "modify_table_seq_scan" not in names, (
        f"UPDATE via Index Scan must not fire, got {names}"
    )


def test_no_false_positive_modify_table_small_seq_scan():
    """ModifyTable with a Seq Scan that only touches a few rows — below threshold, must NOT fire."""
    explain_json = [{
        "Plan": {
            "Node Type": "ModifyTable",
            "Operation": "Update",
            "Relation Name": "txn_statuses",
            "Plan Rows": 12,
            "Actual Rows": 12,
            "Total Cost": 5.0,
            "Actual Total Time": 0.3,
            "Plans": [{
                "Node Type": "Seq Scan",
                "Relation Name": "txn_statuses",
                "Filter": "(code = 'X')",
                "Plan Rows": 12,
                "Actual Rows": 12,
                "Total Cost": 2.0,
                "Actual Total Time": 0.1,
            }],
        },
        "Planning Time": 0.1,
        "Execution Time": 0.4,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "modify_table_seq_scan" not in names, (
        f"small Seq Scan under UPDATE (12 rows < threshold) must not fire, got {names}"
    )


def test_schema_unavailable_coverage_status():
    """
    A schema-dependent skill covering a node type, called with schema_context=None,
    must produce SCHEMA_UNAVAILABLE for that node type — not SKILL_CLEARED or
    NO_APPLICABLE_SKILL. Proves the coverage status distinguishes 'abstained
    because no schema was available' from 'examined and cleared'.
    """
    schema_dep_skill = Skill(
        name="test_schema_dep_coverage",
        description="",
        detects={"node_type": "Sort", "requires_schema_context": True},
        severity="medium",
        explanation="",
        fix_template="",
        covers_node_types=["Sort"],
    )
    explain_json = [{
        "Plan": {
            "Node Type": "Sort",
            "Sort Key": ["x"],
            "Plan Rows": 100,
            "Actual Rows": 100,
            "Total Cost": 100.0,
            "Actual Total Time": 10.0,
        },
        "Planning Time": 0.1,
        "Execution Time": 10.1,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(
        plan, [schema_dep_skill], ledger_status=LedgerStatus.OK, schema_context=None
    )
    assert result.node_type_coverage.get("Sort") == CoverageStatus.SCHEMA_UNAVAILABLE, (
        f"expected SCHEMA_UNAVAILABLE for schema-dependent skill with no schema_context, "
        f"got {result.node_type_coverage}"
    )


def _make_partition_schema(partition_names: list[str], partition_key: list[str]) -> dict:
    """Build schema_context dict keyed by partition table name with a given partition_key."""
    return {
        name: TableSchema(
            table_name=name,
            columns=[],
            indexes=[],
            row_estimate=10000,
            partition_key=partition_key,
        )
        for name in partition_names
    }


def test_append_partition_pruning_failure():
    """
    Append over 4 partitions, Subplans Removed: 0, filter on partition key column
    (created_at), schema_context has partition_key=["created_at"] → must fire.
    Represents scenario 4: enable_partition_pruning=off with a narrow filter.
    """
    partition_names = [
        "transactions_2024_01",
        "transactions_2024_02",
        "transactions_2024_03",
        "transactions_2024_04",
    ]
    explain_json = [{
        "Plan": {
            "Node Type": "Append",
            "Subplans Removed": 0,
            "Plan Rows": 40000,
            "Actual Rows": 40000,
            "Total Cost": 8000.0,
            "Actual Total Time": 600.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": name,
                    "Parent Relationship": "Member",
                    "Filter": "(created_at >= '2024-01-01' AND created_at < '2024-02-01')",
                    "Plan Rows": 10000,
                    "Actual Rows": 10000,
                    "Total Cost": 2000.0,
                    "Actual Total Time": 150.0,
                }
                for name in partition_names
            ],
        },
        "Planning Time": 0.5,
        "Execution Time": 600.5,
    }]
    plan = parse_explain_json(explain_json)
    schema_ctx = _make_partition_schema(partition_names, ["created_at"])
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK, schema_context=schema_ctx)
    names = {m.skill_name for m in result.matches}
    assert "append_partition_pruning_failure" in names, (
        f"expected append_partition_pruning_failure for 4-partition Append with 0 removed "
        f"and filter on partition key, got {names}"
    )


def test_no_false_positive_pruning_filter_not_on_partition_key():
    """
    Append over 4 partitions, Subplans Removed: 0, but filter is on 'merchant'
    (NOT the partition key 'created_at') → skill must NOT fire (scenario 3).
    """
    partition_names = [
        "transactions_2024_01",
        "transactions_2024_02",
        "transactions_2024_03",
        "transactions_2024_04",
    ]
    explain_json = [{
        "Plan": {
            "Node Type": "Append",
            "Subplans Removed": 0,
            "Plan Rows": 40000,
            "Actual Rows": 40000,
            "Total Cost": 8000.0,
            "Actual Total Time": 600.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": name,
                    "Parent Relationship": "Member",
                    "Filter": "(merchant = 'OMV')",
                    "Plan Rows": 10000,
                    "Actual Rows": 10000,
                    "Total Cost": 2000.0,
                    "Actual Total Time": 150.0,
                }
                for name in partition_names
            ],
        },
        "Planning Time": 0.5,
        "Execution Time": 600.5,
    }]
    plan = parse_explain_json(explain_json)
    schema_ctx = _make_partition_schema(partition_names, ["created_at"])
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK, schema_context=schema_ctx)
    names = {m.skill_name for m in result.matches}
    assert "append_partition_pruning_failure" not in names, (
        f"filter on non-partition-key column 'merchant' must not fire, got {names}"
    )


def test_no_false_positive_pruning_no_schema_context():
    """
    Same shape as the positive test but schema_context=None →
    requires_schema_context gate must suppress the skill.
    """
    partition_names = [
        "transactions_2024_01",
        "transactions_2024_02",
        "transactions_2024_03",
        "transactions_2024_04",
    ]
    explain_json = [{
        "Plan": {
            "Node Type": "Append",
            "Subplans Removed": 0,
            "Plan Rows": 40000,
            "Actual Rows": 40000,
            "Total Cost": 8000.0,
            "Actual Total Time": 600.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": name,
                    "Parent Relationship": "Member",
                    "Filter": "(created_at >= '2024-01-01' AND created_at < '2024-02-01')",
                    "Plan Rows": 10000,
                    "Actual Rows": 10000,
                    "Total Cost": 2000.0,
                    "Actual Total Time": 150.0,
                }
                for name in partition_names
            ],
        },
        "Planning Time": 0.5,
        "Execution Time": 600.5,
    }]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK, schema_context=None)
    names = {m.skill_name for m in result.matches}
    assert "append_partition_pruning_failure" not in names, (
        f"schema_context=None must suppress schema-dependent skill, got {names}"
    )


def test_no_false_positive_pruning_few_children():
    """
    Append with only 2 partition children — below min_children guard → must NOT fire.
    Prevents false positives on legitimately small two-partition tables.
    """
    partition_names = ["transactions_2024_01", "transactions_2024_02"]
    explain_json = [{
        "Plan": {
            "Node Type": "Append",
            "Subplans Removed": 0,
            "Plan Rows": 20000,
            "Actual Rows": 20000,
            "Total Cost": 4000.0,
            "Actual Total Time": 300.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": name,
                    "Parent Relationship": "Member",
                    "Filter": "(created_at >= '2024-01-01' AND created_at < '2024-03-01')",
                    "Plan Rows": 10000,
                    "Actual Rows": 10000,
                    "Total Cost": 2000.0,
                    "Actual Total Time": 150.0,
                }
                for name in partition_names
            ],
        },
        "Planning Time": 0.3,
        "Execution Time": 300.3,
    }]
    plan = parse_explain_json(explain_json)
    schema_ctx = _make_partition_schema(partition_names, ["created_at"])
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK, schema_context=schema_ctx)
    names = {m.skill_name for m in result.matches}
    assert "append_partition_pruning_failure" not in names, (
        f"only 2 partitions (< min_children) must not fire, got {names}"
    )


def test_no_false_positive_pruning_high_prune_ratio():
    """
    Append with Subplans Removed: 3 and 1 child remaining — pruning ratio 3/4 = 0.75
    exceeds max_pruning_ratio, so the planner DID prune aggressively → must NOT fire.
    """
    partition_names = ["transactions_2024_04"]
    explain_json = [{
        "Plan": {
            "Node Type": "Append",
            "Subplans Removed": 3,
            "Plan Rows": 10000,
            "Actual Rows": 10000,
            "Total Cost": 2000.0,
            "Actual Total Time": 150.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions_2024_04",
                    "Parent Relationship": "Member",
                    "Filter": "(created_at >= '2024-04-01' AND created_at < '2024-05-01')",
                    "Plan Rows": 10000,
                    "Actual Rows": 10000,
                    "Total Cost": 2000.0,
                    "Actual Total Time": 150.0,
                }
            ],
        },
        "Planning Time": 0.2,
        "Execution Time": 150.2,
    }]
    plan = parse_explain_json(explain_json)
    all_partition_names = [
        "transactions_2024_01", "transactions_2024_02",
        "transactions_2024_03", "transactions_2024_04",
    ]
    schema_ctx = _make_partition_schema(all_partition_names, ["created_at"])
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK, schema_context=schema_ctx)
    names = {m.skill_name for m in result.matches}
    assert "append_partition_pruning_failure" not in names, (
        f"high pruning ratio (3/4 removed) must not fire — pruning worked, got {names}"
    )


def _unique_sort_plan(
    unique_actual_rows: int,
    sort_actual_rows: int,
    sort_child_node_type: str = "Seq Scan",
    unique_parent_relationship: str | None = None,
) -> list:
    """Build a minimal Unique→Sort→<child> EXPLAIN fixture."""
    child = {
        "Node Type": sort_child_node_type,
        "Plan Rows": sort_actual_rows,
        "Actual Rows": sort_actual_rows,
        "Total Cost": 200.0,
        "Actual Total Time": 20.0,
    }
    if sort_child_node_type == "Seq Scan":
        child["Relation Name"] = "transactions"
    if sort_child_node_type == "Append":
        child["Plans"] = [
            {
                "Node Type": "Seq Scan",
                "Relation Name": "transactions_a",
                "Parent Relationship": "Member",
                "Plan Rows": sort_actual_rows // 2,
                "Actual Rows": sort_actual_rows // 2,
                "Total Cost": 100.0,
                "Actual Total Time": 10.0,
            },
            {
                "Node Type": "Seq Scan",
                "Relation Name": "transactions_b",
                "Parent Relationship": "Member",
                "Plan Rows": sort_actual_rows // 2,
                "Actual Rows": sort_actual_rows // 2,
                "Total Cost": 100.0,
                "Actual Total Time": 10.0,
            },
        ]
    unique_node: dict = {
        "Node Type": "Unique",
        "Plan Rows": unique_actual_rows,
        "Actual Rows": unique_actual_rows,
        "Total Cost": 500.0,
        "Actual Total Time": 45.0,
        "Plans": [{
            "Node Type": "Sort",
            "Sort Key": ["merchant"],
            "Plan Rows": sort_actual_rows,
            "Actual Rows": sort_actual_rows,
            "Total Cost": 450.0,
            "Actual Total Time": 40.0,
            "Plans": [child],
        }],
    }
    if unique_parent_relationship is not None:
        unique_node["Parent Relationship"] = unique_parent_relationship
    return [{
        "Plan": unique_node,
        "Planning Time": 0.5,
        "Execution Time": 45.5,
    }]


def test_unique_sort_noop():
    """
    Unique over Sort where 950 of 1000 sorted rows survived dedup (ratio 0.95 >= 0.9)
    and Sort processed 1000 rows (>= child_min_actual_rows 1000) — must fire.
    """
    plan = parse_explain_json(_unique_sort_plan(unique_actual_rows=950, sort_actual_rows=1000))
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "unique_sort_noop" in names, (
        f"expected unique_sort_noop for Unique/Sort with ratio 0.95 on 1000 rows, got {names}"
    )


def test_no_false_positive_unique_sort_noop_low_ratio():
    """
    Unique over Sort where only 200 of 1000 rows survived dedup (ratio 0.2 < 0.9)
    — dedup did real work, must NOT fire.
    """
    plan = parse_explain_json(_unique_sort_plan(unique_actual_rows=200, sort_actual_rows=1000))
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "unique_sort_noop" not in names, (
        f"dedup removed 80% of rows — not wasteful, must not fire, got {names}"
    )


def test_no_false_positive_unique_sort_noop_small_table():
    """
    Unique over Sort on only 100 rows total — below child_min_actual_rows 1000,
    must NOT fire regardless of dedup ratio.
    """
    plan = parse_explain_json(_unique_sort_plan(unique_actual_rows=95, sort_actual_rows=100))
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "unique_sort_noop" not in names, (
        f"only 100 rows sorted — below threshold, must not fire, got {names}"
    )


def test_no_false_positive_unique_sort_noop_union():
    """
    Unique→Sort→Append shape (UNION dedup, not SELECT DISTINCT) — sort_child_not_append
    guard must suppress the skill even though the dedup ratio is high.
    """
    plan = parse_explain_json(
        _unique_sort_plan(
            unique_actual_rows=950,
            sort_actual_rows=1000,
            sort_child_node_type="Append",
        )
    )
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "unique_sort_noop" not in names, (
        f"Unique→Sort→Append is UNION dedup, not SELECT DISTINCT — must not fire, got {names}"
    )


def test_no_false_positive_unique_sort_noop_merge_join_inner():
    """
    Unique with parent_relationship='Inner' is the inner side of a Merge Join —
    parent_relationship_exclude: ['Inner'] must suppress the skill.
    """
    plan = parse_explain_json(
        _unique_sort_plan(
            unique_actual_rows=950,
            sort_actual_rows=1000,
            unique_parent_relationship="Inner",
        )
    )
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "unique_sort_noop" not in names, (
        f"Unique with parent_relationship='Inner' is Merge Join inner side — must not fire, got {names}"
    )


def _cte_scan_plan(
    cte_name: str,
    actual_rows: int,
    second_ref: bool = False,
    include_worktable: bool = False,
) -> list:
    """Build a plan with one or two CTE Scan nodes plus an optional WorkTable Scan sibling."""
    cte_node = {
        "Node Type": "CTE Scan",
        "CTE Name": cte_name,
        "Plan Rows": actual_rows,
        "Actual Rows": actual_rows,
        "Total Cost": 500.0,
        "Actual Total Time": 45.0,
    }
    if include_worktable:
        cte_node["Plans"] = [{
            "Node Type": "WorkTable Scan",
            "CTE Name": cte_name,
            "Parent Relationship": "Inner",
            "Plan Rows": actual_rows,
            "Actual Rows": actual_rows,
            "Total Cost": 100.0,
            "Actual Total Time": 10.0,
        }]
    if not second_ref and not include_worktable:
        return [{"Plan": cte_node, "Planning Time": 0.5, "Execution Time": 45.5}]
    plans = [cte_node]
    if second_ref:
        plans.append({
            "Node Type": "CTE Scan",
            "CTE Name": cte_name,
            "Parent Relationship": "Inner",
            "Plan Rows": actual_rows,
            "Actual Rows": actual_rows,
            "Total Cost": 500.0,
            "Actual Total Time": 45.0,
        })
    return [{
        "Plan": {
            "Node Type": "Nested Loop",
            "Plan Rows": actual_rows,
            "Actual Rows": actual_rows,
            "Total Cost": 1000.0,
            "Actual Total Time": 90.0,
            "Plans": plans,
        },
        "Planning Time": 0.5,
        "Execution Time": 90.5,
    }]


def test_cte_scan_single_ref():
    """
    Single-reference CTE Scan reading 15,000 rows — cte_reference_count == 1
    and min_actual_rows == 10,000 both satisfied. Must fire.
    """
    plan = parse_explain_json(_cte_scan_plan("expensive_cte", actual_rows=15000))
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "cte_scan_single_ref" in names, (
        f"expected cte_scan_single_ref for single-reference large CTE Scan, got {names}"
    )


def test_no_false_positive_cte_scan_two_references():
    """
    Two CTE Scan nodes sharing the same CTE name — cte_reference_count == 2,
    exceeds max_cte_reference_count: 1. Neither must fire.
    """
    plan = parse_explain_json(
        _cte_scan_plan("shared_cte", actual_rows=15000, second_ref=True)
    )
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "cte_scan_single_ref" not in names, (
        f"two references means materialization is intentional — must not fire, got {names}"
    )


def test_no_false_positive_cte_scan_small_rows():
    """
    Single-reference CTE Scan reading only 500 rows — below min_actual_rows: 10000.
    Must NOT fire.
    """
    plan = parse_explain_json(_cte_scan_plan("tiny_cte", actual_rows=500))
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "cte_scan_single_ref" not in names, (
        f"only 500 rows — below threshold, must not fire, got {names}"
    )


def test_cte_scan_with_worktable_sibling():
    """
    CTE Scan (single reference, 15k rows) with a WorkTable Scan child sharing the same
    CTE name. The WorkTable Scan must be excluded from the reference count so the outer
    CTE Scan still gets cte_reference_count == 1 and fires. The WorkTable Scan itself
    must never fire (gated by node_type: 'CTE Scan' in the skill detects).
    """
    plan = parse_explain_json(
        _cte_scan_plan("counter", actual_rows=15000, include_worktable=True)
    )
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "cte_scan_single_ref" in names, (
        f"WorkTable Scan must not inflate ref count — outer CTE Scan should still fire, got {names}"
    )
    # Verify the WorkTable Scan node itself is not in the matches
    matched_node_types = {
        m.matched_node.node_type for m in result.matches if m.matched_node is not None
    }
    assert "WorkTable Scan" not in matched_node_types, (
        f"WorkTable Scan must never match cte_scan_single_ref, got matched types {matched_node_types}"
    )


def _window_agg_plan(sort_actual_rows: int, use_index_scan: bool = False) -> list:
    """Build a minimal WindowAgg → Sort|IndexScan → [scan] EXPLAIN fixture."""
    if use_index_scan:
        child = {
            "Node Type": "Index Scan",
            "Relation Name": "transactions",
            "Index Name": "idx_transactions_account_id_txn_date",
            "Index Cond": "(account_id IS NOT NULL)",
            "Plan Rows": sort_actual_rows,
            "Actual Rows": sort_actual_rows,
            "Total Cost": 2000.0,
            "Actual Total Time": 80.0,
        }
    else:
        child = {
            "Node Type": "Sort",
            "Sort Key": ["account_id", "txn_date"],
            "Sort Method": "quicksort",
            "Sort Space Used": 8192,
            "Sort Space Type": "Memory",
            "Plan Rows": sort_actual_rows,
            "Actual Rows": sort_actual_rows,
            "Total Cost": 8000.0,
            "Actual Total Time": 600.0,
            "Plans": [{
                "Node Type": "Seq Scan",
                "Relation Name": "transactions",
                "Plan Rows": sort_actual_rows,
                "Actual Rows": sort_actual_rows,
                "Total Cost": 4000.0,
                "Actual Total Time": 300.0,
            }],
        }
    return [{
        "Plan": {
            "Node Type": "WindowAgg",
            "Plan Rows": sort_actual_rows,
            "Actual Rows": sort_actual_rows,
            "Total Cost": 10000.0,
            "Actual Total Time": 800.0,
            "Plans": [child],
        },
        "Planning Time": 0.5,
        "Execution Time": 800.5,
    }]


def test_window_agg_sort():
    """
    WindowAgg → Sort → Seq Scan with Sort.actual_rows = 50,000 — must fire.
    Represents running totals per account over a large transactions table.
    """
    plan = parse_explain_json(_window_agg_plan(sort_actual_rows=50000))
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "window_agg_sort" in names, (
        f"expected window_agg_sort for WindowAgg→Sort with 50000 rows, got {names}"
    )


def test_no_false_positive_window_agg_sort_index_scan_child():
    """
    WindowAgg → Index Scan (no Sort) — planner used an ordered index to provide
    the window's order; no explicit sort needed. child_node_type: ["Sort"] gate
    must block the skill even though Sort.actual_rows would be large.
    """
    plan = parse_explain_json(_window_agg_plan(sort_actual_rows=50000, use_index_scan=True))
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "window_agg_sort" not in names, (
        f"WindowAgg with Index Scan child skipped the sort — must not fire, got {names}"
    )


def test_no_false_positive_window_agg_sort_small_rows():
    """
    WindowAgg → Sort → Seq Scan with only 500 rows in Sort — below
    child_min_actual_rows threshold of 10,000. Must NOT fire.
    """
    plan = parse_explain_json(_window_agg_plan(sort_actual_rows=500))
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
    names = {m.skill_name for m in result.matches}
    assert "window_agg_sort" not in names, (
        f"only 500 rows sorted — below threshold, must not fire, got {names}"
    )


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
    test_repeated_seq_scan_in_loop()
    test_single_loop_seq_scan_not_flagged_as_repeated()
    print("\nAll tests passed.")
