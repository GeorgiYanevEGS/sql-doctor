"""
Integration test: the one place in the suite that exercises the real
production wiring — load_skills() with DEFAULT_LEDGER_PATH, the actual
committed ledger, the actual skills/*.yaml definitions.

This test fails if:
- Someone edits a skill's detects block such that it now fires on a fixture
  that used to be a verified negative (ledger entry exists but skill now matches)
- Someone edits covers_node_types without regenerating the ledger
- The committed ledger file is missing or corrupt

Every other test declares LedgerStatus.OK explicitly and is intentionally
scoped to matching logic only. This test is the single exception that threads
the real returned ledger_status through unmodified.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.explain_parser import parse_explain_json
from core.skill_matcher import (
    DEFAULT_LEDGER_PATH,
    CoverageStatus,
    LedgerStatus,
    load_skills,
    match_skills,
)


def test_default_ledger_authorizes_current_skills():
    loaded = load_skills(ledger_path=DEFAULT_LEDGER_PATH)

    assert loaded.ledger_status == LedgerStatus.OK, (
        f"Committed ledger at {DEFAULT_LEDGER_PATH} failed to load: {loaded.ledger_status}. "
        f"Run the test suite to regenerate it."
    )

    # --- Seq Scan: verified by missing_index, implicit_type_conversion,
    #     and repeated_seq_scan_in_loop negative tests ---
    plan = parse_explain_json([
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
    ])
    result = match_skills(
        plan,
        loaded.skills,
        table_row_counts={"transactions": 200000},
        ledger_status=loaded.ledger_status,
    )
    assert not result.matches
    assert result.node_type_coverage.get("Seq Scan") == CoverageStatus.SKILL_CLEARED, (
        f"Seq Scan should be SKILL_CLEARED with the committed ledger, "
        f"got {result.node_type_coverage.get('Seq Scan')}"
    )

    # --- Index Scan: verified by stale_statistics negative test ---
    plan = parse_explain_json([
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
    ])
    result = match_skills(
        plan,
        loaded.skills,
        ledger_status=loaded.ledger_status,
    )
    assert not result.matches
    assert result.node_type_coverage.get("Index Scan") == CoverageStatus.SKILL_CLEARED, (
        f"Index Scan should be SKILL_CLEARED with the committed ledger, "
        f"got {result.node_type_coverage.get('Index Scan')}"
    )

    # --- Nested Loop: verified by stale_statistics negative test ---
    plan = parse_explain_json([
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
                    }
                ],
            },
            "Planning Time": 0.3,
            "Execution Time": 3.8,
        }
    ])
    result = match_skills(
        plan,
        loaded.skills,
        ledger_status=loaded.ledger_status,
    )
    assert not result.matches
    assert result.node_type_coverage.get("Nested Loop") == CoverageStatus.SKILL_CLEARED, (
        f"Nested Loop should be SKILL_CLEARED with the committed ledger, "
        f"got {result.node_type_coverage.get('Nested Loop')}"
    )
