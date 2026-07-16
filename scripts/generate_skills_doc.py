"""
Generate SKILLS.md from the skills/*.yaml library.

SKILLS.md is a human-browsable catalog of every deterministic skill, for
colleagues reading the repo on GitHub without running the tool. It is the
same information `sql-doctor list-skills` prints, rendered as markdown.

The catalog is generated FROM the YAML files (via the same loader the tool
uses at runtime), so it can never drift out of sync with the real skills.
Run this after adding or changing any skill:

    python scripts/generate_skills_doc.py

Exits non-zero if it fails to load the skills.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the project root importable when run as a standalone script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from core.skill_matcher import load_skills  # noqa: E402

_OUTPUT = _REPO_ROOT / "SKILLS.md"

# High first — that's the order a reader triaging problems wants.
_SEVERITY_ORDER = ["high", "medium", "low"]
_SEVERITY_HEADING = {
    "high": "High severity",
    "medium": "Medium severity",
    "low": "Low severity",
}

_HEADER = """# sql-doctor skill catalog

> **Auto-generated from `skills/*.yaml` — do not edit directly.**
> Regenerate with `python scripts/generate_skills_doc.py` after adding or
> changing a skill.

Each skill is a deterministic pattern match against the real
`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` output — no LLM, no guessing. This
is the same library `sql-doctor list-skills` prints, grouped by severity.
"""


def _collapse(text: str) -> str:
    """Flatten a YAML folded/multi-line string to a single line for a table cell,
    escaping pipes so they can't break the surrounding markdown table."""
    return " ".join((text or "").split()).replace("|", "\\|")


def _describe(skill) -> str:
    """
    Best available prose for a skill. Most skills carry their text in
    `explanation`, not the optional `description` field, so fall back to it —
    otherwise the catalog would be blank for the majority of skills.
    """
    return _collapse(skill.description) or _collapse(skill.explanation)


def _format_covers(skill) -> str:
    """Render covers_node_types for a table cell, handling the special forms."""
    cov = list(skill.covers_node_types or [])
    if getattr(skill, "scope", "node") == "plan" or cov == ["PLAN_LEVEL"]:
        return "whole-plan check"
    if cov == ["*"]:
        return "all node types"
    if not cov:
        # covers_all_node_types_exempt: plan-shape logic not tied to a node type.
        return "—"
    return ", ".join(f"`{c}`" for c in cov)


def render(skills) -> str:
    by_severity: dict[str, list] = {}
    for s in skills:
        by_severity.setdefault(s.severity, []).append(s)

    # Known severities first in priority order, then any unexpected ones.
    ordered = [s for s in _SEVERITY_ORDER if s in by_severity]
    ordered += sorted(s for s in by_severity if s not in _SEVERITY_ORDER)

    parts = [_HEADER, f"\n**{len(skills)} skills** across {len(ordered)} severity level(s).\n"]

    for severity in ordered:
        heading = _SEVERITY_HEADING.get(severity, severity.capitalize())
        parts.append(f"\n## {heading}\n")
        parts.append("| Skill | Covers | Description |")
        parts.append("|-------|--------|-------------|")
        for skill in sorted(by_severity[severity], key=lambda s: s.name):
            parts.append(f"| `{skill.name}` | {_format_covers(skill)} | {_describe(skill)} |")

    return "\n".join(parts) + "\n"


def main() -> int:
    loaded = load_skills()
    if not loaded.skills:
        print("ERROR: no skills loaded — nothing to document.", file=sys.stderr)
        return 1
    _OUTPUT.write_text(render(loaded.skills), encoding="utf-8")
    print(f"Wrote {_OUTPUT.relative_to(_REPO_ROOT)} ({len(loaded.skills)} skills).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
