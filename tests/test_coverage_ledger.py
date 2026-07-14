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
