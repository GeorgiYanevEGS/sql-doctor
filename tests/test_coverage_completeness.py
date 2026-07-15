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


# Skills that claim covers_node_types: ["*"] are normally checked against every
# node type in the ledger — O(skills × node_types) growth. Instead, each "*"
# skill below is checked against a fixed representative set covering the three
# structural categories (leaf scan, two-child join, single-child processing).
# This set is frozen and does not grow when other skills add new node types.
#
# TODO: if a THIRD "*" skill appears, replace this dict with a general
# "scope: universal" YAML field and update check_completeness() to read it,
# rather than extending this dict further.
_REPRESENTATIVE_TYPES: frozenset[str] = frozenset({
    "Seq Scan",    # leaf scan
    "Index Scan",  # leaf scan
    "Nested Loop", # two-child join
    "Hash",        # single-child processing (build side of Hash Join)
    "Sort",        # single-child processing
})

_FIXED_COVERAGE_STAR_SKILLS: frozenset[str] = frozenset({
    "stale_statistics",
    "empty_result_bad_estimate",
})


def check_completeness(
    skills: list[Skill],
    ledger_entries: list[dict],
) -> list[tuple[str, str]]:
    """
    Return (skill_name, node_type) pairs that are declared in covers_node_types
    but absent from the ledger. An empty list means the ledger is complete.

    For "*" skills: normally checked against every node_type that appears in
    the ledger for any skill ("all known node types"). Skills in
    _FIXED_COVERAGE_STAR_SKILLS are checked against _REPRESENTATIVE_TYPES only,
    to avoid O(skills × node_types) growth as the skill suite expands.
    """
    ledger_pairs = {(e["skill_name"], e["node_type"]) for e in ledger_entries}
    known_node_types = {e["node_type"] for e in ledger_entries}

    missing = []
    for skill in skills:
        if not skill.covers_node_types:
            if not skill.covers_all_node_types_exempt:
                missing.append((skill.name, "<covers_all_node_types_exempt: true required>"))
            continue
        if skill.covers_node_types == ["*"]:
            if skill.name in _FIXED_COVERAGE_STAR_SKILLS:
                node_types_to_check = _REPRESENTATIVE_TYPES
            else:
                node_types_to_check = known_node_types
            for node_type in node_types_to_check:
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


def _make_skill(name: str, covers: list[str], *, exempt: bool = False) -> Skill:
    return Skill(
        name=name,
        description="",
        detects={},
        severity="high",
        explanation="",
        fix_template="",
        covers_node_types=covers,
        covers_all_node_types_exempt=exempt,
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


def test_empty_covers_without_exempt_flag_fails_completeness():
    """covers_node_types: [] without covers_all_node_types_exempt: true is an error, not a silent pass."""
    skill = _make_skill("accidental_empty", [])
    missing = check_completeness([skill], [])
    assert ("accidental_empty", "<covers_all_node_types_exempt: true required>") in missing, (
        f"expected completeness error for skill with bare covers_node_types: [], got {missing}"
    )


def test_skill_with_empty_covers_node_types_is_ignored():
    """A skill with covers_node_types: [] AND covers_all_node_types_exempt: true is correctly exempt."""
    skill = _make_skill("unconfigured", [], exempt=True)
    missing = check_completeness([skill], [])
    assert missing == []


def test_fixed_coverage_star_skills_not_required_for_new_node_types():
    """
    Skills in _FIXED_COVERAGE_STAR_SKILLS are checked against _REPRESENTATIVE_TYPES
    only. A new node type added by another skill does NOT cascade to them.
    """
    stale_skill = _make_skill("stale_statistics", ["*"])
    empty_skill = _make_skill("empty_result_bad_estimate", ["*"])
    ledger = [
        {"skill_name": "stale_statistics", "node_type": "Seq Scan"},
        {"skill_name": "stale_statistics", "node_type": "Index Scan"},
        {"skill_name": "stale_statistics", "node_type": "Nested Loop"},
        {"skill_name": "stale_statistics", "node_type": "Hash"},
        {"skill_name": "stale_statistics", "node_type": "Sort"},
        {"skill_name": "empty_result_bad_estimate", "node_type": "Seq Scan"},
        {"skill_name": "empty_result_bad_estimate", "node_type": "Index Scan"},
        {"skill_name": "empty_result_bad_estimate", "node_type": "Nested Loop"},
        {"skill_name": "empty_result_bad_estimate", "node_type": "Hash"},
        {"skill_name": "empty_result_bad_estimate", "node_type": "Sort"},
        # Another skill introduces a new node type — must NOT cascade.
        {"skill_name": "other_skill", "node_type": "Index Only Scan"},
    ]
    missing = check_completeness([stale_skill, empty_skill], ledger)
    assert ("stale_statistics", "Index Only Scan") not in missing
    assert ("empty_result_bad_estimate", "Index Only Scan") not in missing


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
