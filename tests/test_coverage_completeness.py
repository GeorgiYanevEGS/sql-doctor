"""
Completeness check: every (skill, node_type) pair declared in a skill's
covers_node_types must have a corresponding entry in the coverage ledger.

This is the check that closes the gap regenerate-and-diff cannot: a newly
declared covers_node_types entry with zero negative tests doesn't change the
ledger (there was never an entry, there still isn't one), so diff sees
nothing. This check catches it by asserting declared ⊆ ledger.

For skills with covers_node_types: ["*"], the skill is checked against every
node type that appears anywhere in the ledger (the "all known node types"
set). If any other skill has been tested against, say, "Hash Join", then
every "*" skill must also have a "Hash Join" ledger entry.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.skill_matcher import DEFAULT_LEDGER_PATH, Skill, load_skills


def check_completeness(
    skills: list[Skill],
    ledger_entries: list[dict],
) -> list[tuple[str, str]]:
    """
    Return (skill_name, node_type) pairs that are declared in covers_node_types
    but absent from the ledger. An empty list means the ledger is complete.

    For "*" skills: checked against every node_type that appears in the ledger
    for any skill ("all known node types"). This enforces that a wildcard claim
    is backed by at least as many negative tests as the most-tested node types.
    """
    ledger_pairs = {(e["skill_name"], e["node_type"]) for e in ledger_entries}
    known_node_types = {e["node_type"] for e in ledger_entries}

    missing = []
    for skill in skills:
        if not skill.covers_node_types:
            continue
        if skill.covers_node_types == ["*"]:
            for node_type in known_node_types:
                if (skill.name, node_type) not in ledger_pairs:
                    missing.append((skill.name, node_type))
        else:
            for node_type in skill.covers_node_types:
                if (skill.name, node_type) not in ledger_pairs:
                    missing.append((skill.name, node_type))
    return missing


# ---------------------------------------------------------------------------
# Unit tests with synthetic skills — isolate the logic from the real ledger
# ---------------------------------------------------------------------------


def _make_skill(name: str, covers: list[str]) -> Skill:
    return Skill(
        name=name,
        description="",
        detects={},
        severity="high",
        explanation="",
        fix_template="",
        covers_node_types=covers,
    )


def test_explicit_skill_missing_from_ledger():
    """Skill declares Seq Scan coverage; empty ledger → reported as missing."""
    skill = _make_skill("test_skill", ["Seq Scan"])
    missing = check_completeness([skill], [])
    assert ("test_skill", "Seq Scan") in missing


def test_explicit_skill_present_in_ledger():
    """Skill declares Seq Scan; ledger has the entry → no missing pairs."""
    skill = _make_skill("test_skill", ["Seq Scan"])
    ledger = [{"skill_name": "test_skill", "node_type": "Seq Scan"}]
    missing = check_completeness([skill], ledger)
    assert missing == []


def test_explicit_skill_partial_coverage():
    """Skill declares two node types; ledger has one → missing the other."""
    skill = _make_skill("test_skill", ["Seq Scan", "Index Scan"])
    ledger = [{"skill_name": "test_skill", "node_type": "Seq Scan"}]
    missing = check_completeness([skill], ledger)
    assert ("test_skill", "Index Scan") in missing
    assert ("test_skill", "Seq Scan") not in missing


def test_star_skill_missing_for_known_node_type():
    """
    A "*" skill must have an entry for every node_type that appears in the
    ledger (the 'all known node types' set). If another skill has been tested
    against "Index Scan", the "*" skill must be too.
    """
    star_skill = _make_skill("star_skill", ["*"])
    ledger = [{"skill_name": "other_skill", "node_type": "Index Scan"}]
    missing = check_completeness([star_skill], ledger)
    assert ("star_skill", "Index Scan") in missing


def test_star_skill_covered_for_all_known_node_types():
    """A "*" skill with ledger entries for every known node type → no missing pairs."""
    star_skill = _make_skill("star_skill", ["*"])
    ledger = [
        {"skill_name": "other_skill", "node_type": "Index Scan"},
        {"skill_name": "star_skill", "node_type": "Index Scan"},
    ]
    missing = check_completeness([star_skill], ledger)
    assert missing == []


def test_skill_with_empty_covers_node_types_is_ignored():
    """A skill with no covers_node_types declaration contributes nothing to completeness."""
    skill = _make_skill("unconfigured", [])
    missing = check_completeness([skill], [])
    assert missing == []


# ---------------------------------------------------------------------------
# Integration test against the real committed ledger
# ---------------------------------------------------------------------------


def test_real_skills_and_ledger_are_complete():
    """
    The committed coverage ledger must have an entry for every (skill, node_type)
    pair declared across all skill YAMLs. This is the change-detector-independent
    check: it catches a new covers_node_types declaration with no negative test,
    which regenerate-and-diff would miss entirely (nothing changed in the ledger).
    """
    loaded = load_skills(ledger_path=DEFAULT_LEDGER_PATH)
    ledger = json.loads(DEFAULT_LEDGER_PATH.read_text(encoding="utf-8"))

    missing = check_completeness(loaded.skills, ledger)

    assert missing == [], (
        "Coverage ledger is incomplete. Declared coverage with no negative test:\n"
        + "\n".join(f"  skill={s!r}  node_type={n!r}" for s, n in sorted(missing))
        + "\n\nRun: pytest tests/test_coverage_ledger.py"
        + "\nThen: git add tests/coverage_ledger.json && git commit"
    )
