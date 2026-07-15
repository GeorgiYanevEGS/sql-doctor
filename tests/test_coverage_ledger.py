"""
Negative tests that populate the coverage ledger (tests/coverage_ledger.json).

Each test calls assert_no_match() against a fixture where the named skill
genuinely should not fire, proving the skill's coverage claim is backed by
a real negative example — not just declared in YAML.

Rules per the coverage contract:
  - Every fixture must contain at least one node of the claimed node_type
    (assert_no_match raises VacuousTestError otherwise).
  - The skill must not fire on any of those nodes (genuine negative).
  - On success, assert_no_match writes (skill_name, node_type) to the
    default ledger at tests/coverage_ledger.json.

Run this file to regenerate the ledger after adding or modifying skills.
CI should verify the committed ledger matches the regenerated one.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.skill_matcher import load_skills
from tests.coverage_helpers import assert_no_match

SKILLS = load_skills().skills

pytestmark = pytest.mark.serial


# ---------------------------------------------------------------------------
# empty_result_bad_estimate
# ---------------------------------------------------------------------------
# Each fixture has plan_rows=1000, actual_rows=900 (ratio=0.9 > 0.1 threshold)
# — the estimate was accurate, so the skill must not fire.


def test_negative_empty_result_bad_estimate_accurate_seq_scan():
    """Seq Scan where actual rows ≈ plan rows — not a bad estimate. Registers ledger entry."""
    assert_no_match(
        "empty_result_bad_estimate",
        "Seq Scan",
        [{
            "Plan": {
                "Node Type": "Seq Scan",
                "Relation Name": "transactions",
                "Plan Rows": 1000,
                "Actual Rows": 900,
                "Total Cost": 4000.0,
                "Actual Total Time": 25.0,
            },
            "Planning Time": 0.3,
            "Execution Time": 25.3,
        }],
        SKILLS,
    )


def test_negative_empty_result_bad_estimate_accurate_index_scan():
    """Index Scan where actual rows ≈ plan rows. Registers ledger entry."""
    assert_no_match(
        "empty_result_bad_estimate",
        "Index Scan",
        [{
            "Plan": {
                "Node Type": "Index Scan",
                "Relation Name": "transactions",
                "Index Name": "idx_transactions_account_id",
                "Index Cond": "(account_id = 42)",
                "Plan Rows": 200,
                "Actual Rows": 195,
                "Total Cost": 50.0,
                "Actual Total Time": 2.0,
            },
            "Planning Time": 0.1,
            "Execution Time": 2.1,
        }],
        SKILLS,
    )


def test_negative_empty_result_bad_estimate_accurate_nested_loop():
    """Nested Loop where actual rows ≈ plan rows. Registers ledger entry."""
    assert_no_match(
        "empty_result_bad_estimate",
        "Nested Loop",
        [{
            "Plan": {
                "Node Type": "Nested Loop",
                "Plan Rows": 500,
                "Actual Rows": 480,
                "Total Cost": 800.0,
                "Actual Total Time": 30.0,
                "Plans": [
                    {
                        "Node Type": "Seq Scan",
                        "Relation Name": "transactions",
                        "Plan Rows": 500,
                        "Actual Rows": 480,
                        "Total Cost": 400.0,
                        "Actual Total Time": 15.0,
                    },
                    {
                        "Node Type": "Index Scan",
                        "Relation Name": "accounts",
                        "Index Name": "accounts_pkey",
                        "Plan Rows": 1,
                        "Actual Rows": 1,
                        "Total Cost": 8.0,
                        "Actual Total Time": 0.03,
                    },
                ],
            },
            "Planning Time": 0.2,
            "Execution Time": 30.2,
        }],
        SKILLS,
    )


def test_negative_empty_result_bad_estimate_accurate_hash():
    """Hash node where actual rows ≈ plan rows. Registers ledger entry."""
    assert_no_match(
        "empty_result_bad_estimate",
        "Hash",
        [{
            "Plan": {
                "Node Type": "Hash Join",
                "Hash Cond": "(t.merchant_id = m.id)",
                "Plan Rows": 1000,
                "Actual Rows": 950,
                "Total Cost": 500.0,
                "Actual Total Time": 40.0,
                "Plans": [
                    {
                        "Node Type": "Seq Scan",
                        "Relation Name": "transactions",
                        "Plan Rows": 1000,
                        "Actual Rows": 950,
                        "Total Cost": 300.0,
                        "Actual Total Time": 25.0,
                    },
                    {
                        "Node Type": "Hash",
                        "Plan Rows": 500,
                        "Actual Rows": 490,
                        "Total Cost": 100.0,
                        "Actual Total Time": 8.0,
                        "Hash Batches": 1,
                        "Original Hash Batches": 1,
                        "Hash Buckets": 1024,
                        "Original Hash Buckets": 1024,
                        "Peak Memory Usage": 4096,
                    },
                ],
            },
            "Planning Time": 0.3,
            "Execution Time": 40.3,
        }],
        SKILLS,
    )


def test_negative_empty_result_bad_estimate_accurate_sort():
    """Sort node where actual rows ≈ plan rows. Registers ledger entry."""
    assert_no_match(
        "empty_result_bad_estimate",
        "Sort",
        [{
            "Plan": {
                "Node Type": "Sort",
                "Sort Key": ["created_at DESC"],
                "Sort Method": "quicksort",
                "Sort Space Used": 512,
                "Sort Space Type": "Memory",
                "Plan Rows": 1000,
                "Actual Rows": 920,
                "Total Cost": 800.0,
                "Actual Total Time": 15.0,
                "Plans": [{
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions",
                    "Plan Rows": 1000,
                    "Actual Rows": 920,
                    "Total Cost": 600.0,
                    "Actual Total Time": 12.0,
                }],
            },
            "Planning Time": 0.2,
            "Execution Time": 15.2,
        }],
        SKILLS,
    )


# ---------------------------------------------------------------------------
# index_only_scan_heap_fetches
# ---------------------------------------------------------------------------


def test_negative_index_only_scan_heap_fetches_low_ratio():
    """
    Index Only Scan with heap_fetches << actual_rows (ratio = 1%): visibility
    map is working, no problem. index_only_scan_heap_fetches must not fire.
    Registers (index_only_scan_heap_fetches, Index Only Scan) in the ledger.
    """
    assert_no_match(
        "index_only_scan_heap_fetches",
        "Index Only Scan",
        [
            {
                "Plan": {
                    "Node Type": "Index Only Scan",
                    "Relation Name": "transactions",
                    "Index Name": "idx_transactions_account_id",
                    "Index Cond": "(account_id = 42)",
                    "Heap Fetches": 10,
                    "Plan Rows": 1000,
                    "Actual Rows": 1000,
                    "Total Cost": 200.0,
                    "Actual Total Time": 8.0,
                },
                "Planning Time": 0.2,
                "Execution Time": 8.2,
            }
        ],
        SKILLS,
    )


# ---------------------------------------------------------------------------
# nested_loop_bad_plan
# ---------------------------------------------------------------------------


def test_negative_nested_loop_bad_plan_good_estimate():
    """
    Nested Loop where the outer child's row estimate is accurate (plan_rows=100,
    actual_rows=110, ratio≈1.1x — well below the 10x threshold). Inner child is
    an Index Scan so the shape matches, but the estimate is fine.
    nested_loop_bad_plan must not fire.
    Registers (nested_loop_bad_plan, Nested Loop) in the ledger.
    """
    assert_no_match(
        "nested_loop_bad_plan",
        "Nested Loop",
        [
            {
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
            }
        ],
        SKILLS,
    )


# ---------------------------------------------------------------------------
# join_condition_function_wrap
# ---------------------------------------------------------------------------


def test_negative_join_condition_function_wrap_hash_join_plain():
    """
    Hash Join with a plain column equality (no function) — join_condition_function_wrap
    must not fire.
    Registers (join_condition_function_wrap, Hash Join) in the ledger.
    """
    assert_no_match(
        "join_condition_function_wrap",
        "Hash Join",
        [
            {
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
            }
        ],
        SKILLS,
    )


def test_negative_join_condition_function_wrap_merge_join_plain():
    """
    Merge Join with a plain column equality — join_condition_function_wrap
    must not fire.
    Registers (join_condition_function_wrap, Merge Join) in the ledger.
    """
    assert_no_match(
        "join_condition_function_wrap",
        "Merge Join",
        [
            {
                "Plan": {
                    "Node Type": "Merge Join",
                    "Merge Cond": "(a.id = b.a_id)",
                    "Plan Rows": 2000,
                    "Actual Rows": 2000,
                    "Total Cost": 6000.0,
                    "Actual Total Time": 120.0,
                    "Plans": [
                        {
                            "Node Type": "Sort",
                            "Sort Key": ["a.id"],
                            "Sort Method": "quicksort",
                            "Sort Space Used": 256,
                            "Sort Space Type": "Memory",
                            "Plan Rows": 2000,
                            "Actual Rows": 2000,
                            "Total Cost": 3000.0,
                            "Actual Total Time": 60.0,
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
                            "Sort Key": ["b.a_id"],
                            "Sort Method": "quicksort",
                            "Sort Space Used": 128,
                            "Sort Space Type": "Memory",
                            "Plan Rows": 500,
                            "Actual Rows": 500,
                            "Total Cost": 1000.0,
                            "Actual Total Time": 25.0,
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
                "Planning Time": 0.3,
                "Execution Time": 120.3,
            }
        ],
        SKILLS,
    )


# ---------------------------------------------------------------------------
# repeated_index_scan_in_loop
# ---------------------------------------------------------------------------


def test_negative_repeated_index_scan_in_loop_few_loops():
    """
    Index Scan executing only 10 times (below the 50-loop threshold) —
    repeated_index_scan_in_loop must not fire.
    Registers (repeated_index_scan_in_loop, Index Scan) in the ledger.
    """
    assert_no_match(
        "repeated_index_scan_in_loop",
        "Index Scan",
        [
            {
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
            }
        ],
        SKILLS,
    )


def test_negative_repeated_index_only_scan_in_loop_few_loops():
    """
    Index Only Scan executing only 10 times — repeated_index_scan_in_loop
    must not fire.
    Registers (repeated_index_scan_in_loop, Index Only Scan) in the ledger.
    """
    assert_no_match(
        "repeated_index_scan_in_loop",
        "Index Only Scan",
        [
            {
                "Plan": {
                    "Node Type": "Nested Loop",
                    "Plan Rows": 10,
                    "Actual Rows": 10,
                    "Total Cost": 60.0,
                    "Actual Total Time": 1.5,
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
                            "Node Type": "Index Only Scan",
                            "Relation Name": "accounts",
                            "Index Name": "idx_accounts_id_balance",
                            "Index Cond": "(id = transactions.account_id)",
                            "Heap Fetches": 0,
                            "Plan Rows": 1,
                            "Actual Rows": 1,
                            "Total Cost": 3.0,
                            "Actual Total Time": 0.06,
                            "Actual Loops": 10,
                        },
                    ],
                },
                "Planning Time": 0.1,
                "Execution Time": 1.6,
            }
        ],
        SKILLS,
    )


# ---------------------------------------------------------------------------
# parallel_worker_underutilization
# ---------------------------------------------------------------------------


def test_negative_parallel_worker_underutilization_gather_full():
    """
    Gather with workers_planned=4 and workers_launched=4 — full parallelism
    achieved, no shortfall. parallel_worker_underutilization must not fire.
    Registers (parallel_worker_underutilization, Gather) in the ledger.
    """
    assert_no_match(
        "parallel_worker_underutilization",
        "Gather",
        [
            {
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
            }
        ],
        SKILLS,
    )


def test_negative_parallel_worker_underutilization_gather_merge_full():
    """
    Gather Merge with workers_planned=2 and workers_launched=2 — full
    parallelism, no shortfall. parallel_worker_underutilization must not fire.
    Registers (parallel_worker_underutilization, Gather Merge) in the ledger.
    """
    assert_no_match(
        "parallel_worker_underutilization",
        "Gather Merge",
        [
            {
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
            }
        ],
        SKILLS,
    )


# ---------------------------------------------------------------------------
# missing_index
# ---------------------------------------------------------------------------


def test_negative_missing_index_high_selectivity_seq_scan():
    """
    Seq Scan filtering ~60% of a 200k-row table: selectivity (0.60) exceeds
    missing_index's max_selectivity_ratio (0.25), so a Seq Scan is the
    correct plan and missing_index must stay silent.
    Registers (missing_index, Seq Scan) in the ledger.
    """
    assert_no_match(
        "missing_index",
        "Seq Scan",
        [
            {
                "Plan": {
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions",
                    "Filter": "((txn_type)::text = 'OPER'::text)",
                    "Plan Rows": 120207,
                    "Actual Rows": 119884,
                    "Total Cost": 4606.0,
                    "Actual Total Time": 27.58,
                },
                "Planning Time": 1.13,
                "Execution Time": 30.86,
            }
        ],
        SKILLS,
        table_row_counts={"transactions": 200000},
    )


# ---------------------------------------------------------------------------
# implicit_type_conversion
# ---------------------------------------------------------------------------


def test_negative_implicit_conversion_plain_equality_seq_scan():
    """
    Seq Scan with a plain equality filter — no LOWER/UPPER/TO_CHAR/CAST
    wrapping the column. The '::text' on the literal is PostgreSQL plan
    formatting, not a column-side transformation. Must not be flagged.
    Registers (implicit_type_conversion, Seq Scan) in the ledger.
    """
    assert_no_match(
        "implicit_type_conversion",
        "Seq Scan",
        [
            {
                "Plan": {
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions",
                    "Filter": "(txn_type = 'OPER'::text)",
                    "Plan Rows": 1,
                    "Actual Rows": 7,
                    "Total Cost": 12.5,
                    "Actual Total Time": 0.02,
                },
                "Planning Time": 0.85,
                "Execution Time": 0.03,
            }
        ],
        SKILLS,
    )


# ---------------------------------------------------------------------------
# repeated_seq_scan_in_loop
# ---------------------------------------------------------------------------


def test_negative_repeated_seq_scan_single_loop():
    """
    A plain Seq Scan with Actual Loops = 1 — the normal case where the
    table is read once. repeated_seq_scan_in_loop requires min_actual_loops
    of 50, so it must not fire here.
    Registers (repeated_seq_scan_in_loop, Seq Scan) in the ledger.
    """
    assert_no_match(
        "repeated_seq_scan_in_loop",
        "Seq Scan",
        [
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
        ],
        SKILLS,
        table_row_counts={"transactions": 5000},
    )


# ---------------------------------------------------------------------------
# stale_statistics
# ---------------------------------------------------------------------------


def test_negative_stale_statistics_accurate_seq_scan():
    """
    Seq Scan where Plan Rows is close to Actual Rows (ratio ~1.05, well
    below the 10x threshold). stale_statistics must not fire.
    Registers (stale_statistics, Seq Scan) in the ledger.
    """
    assert_no_match(
        "stale_statistics",
        "Seq Scan",
        [
            {
                "Plan": {
                    "Node Type": "Seq Scan",
                    "Relation Name": "transactions",
                    "Plan Rows": 1000,
                    "Actual Rows": 1050,
                    "Total Cost": 900.0,
                    "Actual Total Time": 15.0,
                },
                "Planning Time": 0.2,
                "Execution Time": 15.2,
            }
        ],
        SKILLS,
    )


def test_negative_stale_statistics_accurate_index_scan():
    """
    Index Scan where the planner estimated 12 rows and got 11 back (ratio
    ~0.92). stale_statistics requires a 10x error — this is a healthy plan.
    Registers (stale_statistics, Index Scan) in the ledger.
    """
    assert_no_match(
        "stale_statistics",
        "Index Scan",
        [
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
        ],
        SKILLS,
    )


def test_negative_stale_statistics_accurate_nested_loop():
    """
    Nested Loop with a well-calibrated row estimate (Plan Rows = 100,
    Actual Rows = 95, ratio ~0.95). stale_statistics must not fire.
    Registers (stale_statistics, Nested Loop) in the ledger.
    """
    assert_no_match(
        "stale_statistics",
        "Nested Loop",
        [
            {
                "Plan": {
                    "Node Type": "Nested Loop",
                    "Relation Name": None,
                    "Plan Rows": 100,
                    "Actual Rows": 95,
                    "Total Cost": 250.0,
                    "Actual Total Time": 3.5,
                    "Plans": [
                        {
                            "Node Type": "Index Scan",
                            "Relation Name": "accounts",
                            "Index Name": "accounts_pkey",
                            "Plan Rows": 10,
                            "Actual Rows": 10,
                            "Total Cost": 50.0,
                            "Actual Total Time": 0.5,
                        },
                        {
                            "Node Type": "Index Scan",
                            "Relation Name": "transactions",
                            "Index Name": "idx_transactions_account_id",
                            "Plan Rows": 10,
                            "Actual Rows": 9,
                            "Total Cost": 20.0,
                            "Actual Total Time": 0.3,
                        },
                    ],
                },
                "Planning Time": 0.3,
                "Execution Time": 3.8,
            }
        ],
        SKILLS,
    )


def test_negative_stale_statistics_accurate_hash():
    """
    Hash node where Plan Rows is close to Actual Rows (ratio ~0.98).
    stale_statistics covers "*" so it must be proven not to fire on Hash nodes.
    Registers (stale_statistics, Hash) in the ledger.
    """
    assert_no_match(
        "stale_statistics",
        "Hash",
        [
            {
                "Plan": {
                    "Node Type": "Hash Join",
                    "Hash Cond": "(t.merchant_id = m.id)",
                    "Plan Rows": 1000,
                    "Actual Rows": 980,
                    "Total Cost": 500.0,
                    "Actual Total Time": 120.0,
                    "Plans": [
                        {
                            "Node Type": "Seq Scan",
                            "Relation Name": "transactions",
                            "Plan Rows": 1000,
                            "Actual Rows": 980,
                            "Total Cost": 300.0,
                            "Actual Total Time": 80.0,
                        },
                        {
                            "Node Type": "Hash",
                            "Plan Rows": 500,
                            "Actual Rows": 490,
                            "Total Cost": 100.0,
                            "Actual Total Time": 20.0,
                            "Hash Batches": 1,
                            "Original Hash Batches": 1,
                            "Hash Buckets": 1024,
                            "Original Hash Buckets": 1024,
                            "Peak Memory Usage": 4096,
                        },
                    ],
                },
                "Planning Time": 0.5,
                "Execution Time": 120.5,
            }
        ],
        SKILLS,
    )


def test_negative_stale_statistics_accurate_sort():
    """
    Sort node where Plan Rows is close to Actual Rows (ratio ~1.01).
    stale_statistics covers "*" so it must be proven not to fire on Sort nodes.
    Registers (stale_statistics, Sort) in the ledger.
    """
    assert_no_match(
        "stale_statistics",
        "Sort",
        [
            {
                "Plan": {
                    "Node Type": "Sort",
                    "Sort Key": ["created_at DESC"],
                    "Sort Method": "quicksort",
                    "Sort Space Used": 512,
                    "Sort Space Type": "Memory",
                    "Plan Rows": 1000,
                    "Actual Rows": 1010,
                    "Total Cost": 800.0,
                    "Actual Total Time": 15.0,
                    "Plans": [
                        {
                            "Node Type": "Seq Scan",
                            "Relation Name": "transactions",
                            "Plan Rows": 1000,
                            "Actual Rows": 1010,
                            "Total Cost": 600.0,
                            "Actual Total Time": 12.0,
                        }
                    ],
                },
                "Planning Time": 0.2,
                "Execution Time": 15.2,
            }
        ],
        SKILLS,
    )


# ---------------------------------------------------------------------------
# hash_join_disk_spill
# ---------------------------------------------------------------------------


def test_negative_hash_join_disk_spill_no_spill():
    """
    Hash node where Hash Batches == Original Hash Batches (1 == 1): build side
    fit entirely in work_mem. hash_join_disk_spill must not fire.
    Registers (hash_join_disk_spill, Hash) in the ledger.
    """
    assert_no_match(
        "hash_join_disk_spill",
        "Hash",
        [
            {
                "Plan": {
                    "Node Type": "Hash Join",
                    "Hash Cond": "(t.merchant_id = m.id)",
                    "Plan Rows": 50000,
                    "Actual Rows": 45000,
                    "Total Cost": 8500.0,
                    "Actual Total Time": 800.0,
                    "Plans": [
                        {
                            "Node Type": "Seq Scan",
                            "Relation Name": "transactions",
                            "Plan Rows": 50000,
                            "Actual Rows": 45000,
                            "Total Cost": 4000.0,
                            "Actual Total Time": 600.0,
                        },
                        {
                            "Node Type": "Hash",
                            "Plan Rows": 1000,
                            "Actual Rows": 980,
                            "Total Cost": 200.0,
                            "Actual Total Time": 100.0,
                            "Hash Batches": 1,
                            "Original Hash Batches": 1,
                            "Hash Buckets": 1024,
                            "Original Hash Buckets": 1024,
                            "Peak Memory Usage": 4096,
                        },
                    ],
                },
                "Planning Time": 0.5,
                "Execution Time": 800.5,
            }
        ],
        SKILLS,
    )


# ---------------------------------------------------------------------------
# redundant_sort_after_ordered_scan
# ---------------------------------------------------------------------------


def test_negative_redundant_sort_after_ordered_scan_seq_scan_child():
    """
    Sort whose only child is a Seq Scan — Seq Scan output is unordered, so
    the Sort is not redundant. redundant_sort_after_ordered_scan must not fire.
    Registers (redundant_sort_after_ordered_scan, Sort) in the ledger.
    """
    assert_no_match(
        "redundant_sort_after_ordered_scan",
        "Sort",
        [
            {
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
                    "Plans": [
                        {
                            "Node Type": "Seq Scan",
                            "Relation Name": "transactions",
                            "Plan Rows": 1000,
                            "Actual Rows": 1000,
                            "Total Cost": 4000.0,
                            "Actual Total Time": 80.0,
                        }
                    ],
                },
                "Planning Time": 0.3,
                "Execution Time": 100.3,
            }
        ],
        SKILLS,
    )


# ---------------------------------------------------------------------------
# sort_spill_to_disk
# ---------------------------------------------------------------------------


def test_negative_sort_spill_to_disk_in_memory():
    """
    Sort node using quicksort (Sort Method != "external merge"): sort fit in
    work_mem. sort_spill_to_disk must not fire.
    Registers (sort_spill_to_disk, Sort) in the ledger.
    """
    assert_no_match(
        "sort_spill_to_disk",
        "Sort",
        [
            {
                "Plan": {
                    "Node Type": "Sort",
                    "Sort Key": ["created_at DESC"],
                    "Sort Method": "quicksort",
                    "Sort Space Used": 512,
                    "Sort Space Type": "Memory",
                    "Plan Rows": 10000,
                    "Actual Rows": 10000,
                    "Total Cost": 5000.0,
                    "Actual Total Time": 200.0,
                    "Plans": [
                        {
                            "Node Type": "Seq Scan",
                            "Relation Name": "transactions",
                            "Plan Rows": 10000,
                            "Actual Rows": 10000,
                            "Total Cost": 4000.0,
                            "Actual Total Time": 150.0,
                        }
                    ],
                },
                "Planning Time": 0.3,
                "Execution Time": 200.3,
            }
        ],
        SKILLS,
    )
