"""
Loads the YAML skill library and matches each skill's detection rules
against a parsed execution plan.

This whole module runs with zero LLM calls. If a skill matches, the
diagnosis is 100% deterministic and reproducible — the "20 years of
banking ETL bugs, encoded as data" layer described to the user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from core.explain_parser import ParsedPlan, PlanNode

DEFAULT_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


@dataclass
class SkillMatch:
    skill_name: str
    severity: str
    explanation: str
    fix_template: str
    matched_node: PlanNode


@dataclass
class Skill:
    name: str
    description: str
    detects: dict
    severity: str
    explanation: str
    fix_template: str

    @classmethod
    def from_yaml_file(cls, path: Path) -> "Skill":
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            detects=data["detects"],
            severity=data.get("severity", "medium"),
            explanation=data.get("explanation", "").strip(),
            fix_template=data.get("fix_template", "").strip(),
        )

    def matches_node(self, node: PlanNode, table_row_counts: dict[str, int] | None = None) -> bool:
        rules = self.detects
        table_row_counts = table_row_counts or {}

        if "node_type" in rules and node.node_type != rules["node_type"]:
            return False

        if "min_row_estimate_error_ratio" in rules or "max_row_estimate_error_ratio" in rules:
            ratio = node.row_estimate_error_ratio
            if ratio == 0.0:
                return False
            lo = rules.get("min_row_estimate_error_ratio")
            hi = rules.get("max_row_estimate_error_ratio")
            if lo is not None and ratio < lo:
                return False
            if hi is not None and ratio > hi:
                return False

        if "condition_pattern" in rules:
            haystack = " ".join(
                filter(None, [node.filter_condition, node.index_condition])
            )
            if not haystack:
                return False
            if not re.search(rules["condition_pattern"], haystack, re.IGNORECASE):
                return False

        if "requires_no_index" in rules and rules["requires_no_index"]:
            if node.index_name:
                return False

        if "min_actual_rows" in rules and node.actual_rows < rules["min_actual_rows"]:
            return False

        if "max_selectivity_ratio" in rules:
            total_rows = table_row_counts.get(node.relation_name or "")
            # Without a known table size we can't judge selectivity — err
            # on the side of NOT suggesting an index rather than spamming
            # a suggestion the planner may have good reason to ignore.
            if not total_rows or total_rows <= 0:
                return False
            selectivity = node.actual_rows / total_rows
            if selectivity > rules["max_selectivity_ratio"]:
                return False

        return True

    def fix_text(self, node: PlanNode) -> str:
        return self.fix_template.format(
            table=node.relation_name or "<table>",
            index=node.index_name or "<index>",
        )


def load_skills(skills_dir: Path | str = DEFAULT_SKILLS_DIR) -> list[Skill]:
    skills_dir = Path(skills_dir)
    skills = []
    for path in sorted(skills_dir.glob("*.yaml")):
        try:
            skills.append(Skill.from_yaml_file(path))
        except Exception as exc:  # noqa: BLE001
            print(f"[sql-doctor] Warning: failed to load skill {path.name}: {exc}")
    return skills


def match_skills(
    plan: ParsedPlan,
    skills: list[Skill],
    table_row_counts: dict[str, int] | None = None,
) -> list[SkillMatch]:
    matches: list[SkillMatch] = []
    for node in plan.all_nodes():
        for skill in skills:
            if skill.matches_node(node, table_row_counts):
                matches.append(
                    SkillMatch(
                        skill_name=skill.name,
                        severity=skill.severity,
                        explanation=skill.explanation,
                        fix_template=skill.fix_text(node),
                        matched_node=node,
                    )
                )
    # Highest severity first so the CLI shows the most important issue on top.
    order = {"high": 0, "medium": 1, "low": 2}
    matches.sort(key=lambda m: order.get(m.severity, 99))
    return matches
