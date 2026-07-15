"""
Coverage ledger helper for skill negative tests.

assert_no_match() is the canonical way to write a negative test for a skill.
It guards against vacuous tests (plans that don't contain the claimed node
type) and writes a (skill_name, node_type) entry to the ledger on success,
which load_skills() uses at runtime to verify coverage claims.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.explain_parser import parse_explain_json
from core.skill_matcher import Skill

DEFAULT_LEDGER_PATH = Path(__file__).resolve().parent / "coverage_ledger.json"


class VacuousTestError(Exception):
    """Raised when assert_no_match is called with a plan that lacks the claimed node type."""


def assert_no_match(
    skill_name: str,
    node_type: str,
    explain_json: list,
    skills: list[Skill],
    table_row_counts: dict[str, int] | None = None,
    ledger_path: Path | None = None,
) -> None:
    """
    Assert that the named skill does not match any node of node_type in the
    plan fixture.  Raises VacuousTestError if the plan contains no node of
    that type (the test would prove nothing).  Writes a ledger entry on
    success so load_skills() can verify coverage at runtime.
    """
    plan = parse_explain_json(explain_json)

    skill = next((s for s in skills if s.name == skill_name), None)
    if skill is None:
        raise ValueError(f"No skill named {skill_name!r} in provided skill list")

    if node_type == "PLAN_LEVEL":
        # Plan-level skills have no per-node assertion; verify matches_plan() returns False.
        assert not skill.matches_plan(plan), (
            f"assert_no_match({skill_name!r}, 'PLAN_LEVEL'): "
            f"skill.matches_plan() returned True — use a fixture where the skill does not fire."
        )
    else:
        target_nodes = [n for n in plan.all_nodes() if n.node_type == node_type]
        if not target_nodes:
            raise VacuousTestError(
                f"assert_no_match({skill_name!r}, {node_type!r}): "
                f"plan contains no '{node_type}' node — this test is vacuous. "
                f"Use a fixture that actually contains the claimed node type."
            )
        for node in target_nodes:
            assert not skill.matches_node(node, table_row_counts or {}), (
                f"assert_no_match({skill_name!r}, {node_type!r}): "
                f"skill fired on node '{node.node_type}' — this is a positive match, not a negative. "
                f"Use a fixture where the skill genuinely does not fire."
            )

    _write_ledger_entry(skill_name, node_type, ledger_path or DEFAULT_LEDGER_PATH)


def _read_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _write_ledger_entry(skill_name: str, node_type: str, path: Path) -> None:
    # Load existing entries into a dict keyed by (skill_name, node_type), upsert,
    # then serialize in sorted-key order so two runs with the same tests in
    # different execution order produce byte-identical JSON. This makes
    # `git diff tests/coverage_ledger.json` meaningful and CI comparison trivial.
    # NOTE: not safe under pytest-xdist (-n auto) — concurrent writes to the same
    # file race. If the suite ever uses parallel workers, run ledger-writing tests
    # in a dedicated non-parallel group or add a file lock here.
    existing = {
        (e["skill_name"], e["node_type"]): e
        for e in _read_ledger(path)
    }
    existing[(skill_name, node_type)] = {"skill_name": skill_name, "node_type": node_type}
    canonical = [v for _, v in sorted(existing.items())]
    path.write_text(json.dumps(canonical, indent=2), encoding="utf-8")
