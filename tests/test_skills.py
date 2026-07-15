"""
Sanity tests that don't require a real database — they feed synthetic
EXPLAIN JSON straight into the parser and skill matcher, simulating what
psycopg2 would return for three classic anti-patterns.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.explain_parser import parse_explain_json
from core.skill_matcher import CoverageStatus, LedgerStatus, load_skills, match_skills

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
    A Hash Join node with no row estimate error: when stale_statistics (the
    only skill with "*" coverage) is excluded from the skill list, a Hash
    Join has no covering skill and must produce NO_APPLICABLE_SKILL.
    """
    # Exclude stale_statistics so no skill covers Hash Join via "*"
    explicit_skills = [s for s in SKILLS if "*" not in s.covers_node_types]
    explain_json = [
        {
            "Plan": {
                "Node Type": "Hash Join",
                "Relation Name": None,
                "Plan Rows": 100,
                "Actual Rows": 105,
                "Total Cost": 100.0,
                "Actual Total Time": 5.0,
            },
            "Planning Time": 0.1,
            "Execution Time": 5.1,
        }
    ]
    plan = parse_explain_json(explain_json)
    result = match_skills(plan, explicit_skills, ledger_status=LedgerStatus.OK)
    assert result.node_type_coverage.get("Hash Join") == CoverageStatus.NO_APPLICABLE_SKILL


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
    """Sort directly on top of an Index Scan — shape matches, skill fires as heuristic."""
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
    result = match_skills(plan, SKILLS, ledger_status=LedgerStatus.OK)
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
