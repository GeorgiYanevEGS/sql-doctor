"""
Loads the YAML skill library and matches each skill's detection rules
against a parsed execution plan.

This whole module runs with zero LLM calls. If a skill matches, the
diagnosis is 100% deterministic and reproducible — the "20 years of
banking ETL bugs, encoded as data" layer described to the user.
"""

from __future__ import annotations

import importlib.resources
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import yaml

from core.explain_parser import ParsedPlan, PlanNode

DEFAULT_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

# Locate the ledger via the package system rather than a raw __file__ path.
# TODO (future session): promote coverage_ledger.json from tests/ into
# core/data/ and change this reference to:
#   importlib.resources.files("core").joinpath("data/coverage_ledger.json")
# That path is bundled automatically by PyInstaller via package-data datas
# and resolves correctly in both source and frozen-binary contexts.
DEFAULT_LEDGER_PATH = (
    Path(str(importlib.resources.files("core"))).parent / "tests" / "coverage_ledger.json"
)


class CoverageStatus(Enum):
    SKILL_CLEARED = "skill_cleared"
    NO_APPLICABLE_SKILL = "no_applicable_skill"
    UNVERIFIED = "unverified"


class LedgerStatus(Enum):
    OK = "ok"
    MISSING = "missing"
    CORRUPT = "corrupt"


@dataclass
class LoadedSkills:
    skills: list["Skill"]
    ledger_status: LedgerStatus


@dataclass
class DiagnosisResult:
    matches: list["SkillMatch"]
    node_type_coverage: dict[str, CoverageStatus]
    ledger_status: LedgerStatus = field(default_factory=lambda: LedgerStatus.OK)

    @property
    def ledger_load_error(self) -> bool:
        return self.ledger_status != LedgerStatus.OK


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
    covers_node_types: list[str] = field(default_factory=list)
    # Populated by load_skills when a ledger is present; None = no ledger (all trusted).
    _verified_node_types: set[str] | None = field(default=None, repr=False, compare=False)

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
            covers_node_types=data.get("covers_node_types", []),
        )

    def covers(self, node_type: str) -> bool:
        return "*" in self.covers_node_types or node_type in self.covers_node_types

    def is_verified_for(self, node_type: str) -> bool:
        """True when coverage of node_type is backed by a ledger negative test (or no ledger loaded)."""
        if self._verified_node_types is None:
            return True
        return node_type in self._verified_node_types

    def matches_node(self, node: PlanNode, table_row_counts: dict[str, int] | None = None) -> bool:
        rules = self.detects
        table_row_counts = table_row_counts or {}

        if "node_type" in rules and node.node_type != rules["node_type"]:
            return False

        if "min_row_estimate_error_ratio" in rules or "max_row_estimate_error_ratio" in rules:
            # Guard on plan_rows specifically, not on the ratio itself. The old
            # `if ratio == 0.0: return False` guard conflated two distinct cases:
            # plan_rows == 0 (ratio undefined, can't evaluate — should skip) and
            # actual_rows == 0 (ratio is genuinely 0.0, meaningful for low-estimate
            # detection like empty_result_bad_estimate). Guard only the former.
            if node.plan_rows <= 0:
                return False
            ratio = node.row_estimate_error_ratio
            lo = rules.get("min_row_estimate_error_ratio")
            hi = rules.get("max_row_estimate_error_ratio")
            if lo is not None and ratio < lo:
                return False
            if hi is not None and ratio > hi:
                return False

        if "min_plan_rows" in rules and node.plan_rows < rules["min_plan_rows"]:
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

        if "min_actual_loops" in rules and node.actual_loops < rules["min_actual_loops"]:
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

        # Hash join disk spill: build side grew beyond work_mem and batched to disk.
        # Both fields are None on non-Hash nodes, so this rule is safely a no-op there.
        # Root cause shared with requires_sort_spill: work_mem undersized vs. data volume.
        if rules.get("requires_hash_spill"):
            if node.hash_batches is None or node.original_hash_batches is None:
                return False
            if node.hash_batches <= node.original_hash_batches:
                return False

        # Sort spill to disk: sort exceeded work_mem and used external merge.
        # Root cause shared with requires_hash_spill: work_mem undersized vs. data volume.
        if rules.get("requires_sort_spill"):
            if node.sort_method != "external merge":
                return False

        # Child node type predicate: the node must have at least one immediate child
        # whose node_type appears in the allowed list. Enables parent-looks-down-at-child
        # pattern detection without needing a child-to-parent backreference.
        if "child_node_type" in rules:
            allowed = rules["child_node_type"]
            if not any(c.node_type in allowed for c in node.children):
                return False

        return True

    def fix_text(self, node: PlanNode) -> str:
        return self.fix_template.format(
            table=node.relation_name or "<table>",
            index=node.index_name or "<index>",
        )


def load_skills(
    skills_dir: Path | str = DEFAULT_SKILLS_DIR,
    ledger_path: Path | None = None,
) -> LoadedSkills:
    skills_dir = Path(skills_dir)
    skills = []
    for path in sorted(skills_dir.glob("*.yaml")):
        try:
            skills.append(Skill.from_yaml_file(path))
        except Exception as exc:  # noqa: BLE001
            print(f"[sql-doctor] Warning: failed to load skill {path.name}: {exc}")

    if ledger_path is not None:
        ledger_status = _apply_ledger(skills, Path(ledger_path))
    else:
        ledger_status = LedgerStatus.OK

    return LoadedSkills(skills=skills, ledger_status=ledger_status)


def _apply_ledger(skills: list[Skill], ledger_path: Path) -> LedgerStatus:
    """
    Cross-check each skill's covers_node_types against the ledger.
    A (skill_name, node_type) pair not in the ledger is marked unverified —
    the skill can still fire positive matches but won't contribute to SKILL_CLEARED.
    Fails open if the ledger is missing or corrupt: all coverage downgrades to
    unverified, and the returned LedgerStatus tells the caller which failure occurred.
    """
    ledger_status = LedgerStatus.OK
    verified_pairs: set[tuple[str, str]] = set()

    if not ledger_path.exists():
        ledger_status = LedgerStatus.MISSING
    else:
        try:
            entries = json.loads(ledger_path.read_text(encoding="utf-8"))
            verified_pairs = {(e["skill_name"], e["node_type"]) for e in entries}
        except Exception:  # noqa: BLE001
            ledger_status = LedgerStatus.CORRUPT

    for skill in skills:
        if not skill.covers_node_types:
            skill._verified_node_types = set()
            continue

        if ledger_status != LedgerStatus.OK:
            skill._verified_node_types = set()
        elif "*" in skill.covers_node_types:
            # "*" is verified for the specific node types that have a ledger entry
            skill._verified_node_types = {
                node_type for (sn, node_type) in verified_pairs if sn == skill.name
            }
        else:
            skill._verified_node_types = {
                nt for nt in skill.covers_node_types
                if (skill.name, nt) in verified_pairs
            }

    return ledger_status


def match_skills(
    plan: ParsedPlan,
    skills: list[Skill],
    table_row_counts: dict[str, int] | None = None,
    *,
    ledger_status: LedgerStatus,
) -> DiagnosisResult:
    matches: list[SkillMatch] = []
    ever_matched: set[str] = set()
    covered_verified: set[str] = set()    # covered by a verified skill, didn't fire
    covered_unverified: set[str] = set()  # covered only by unverified skills, didn't fire

    for node in plan.all_nodes():
        node_matched = False
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
                node_matched = True

        if node_matched:
            ever_matched.add(node.node_type)
        else:
            for skill in skills:
                if skill.covers(node.node_type):
                    if skill.is_verified_for(node.node_type):
                        covered_verified.add(node.node_type)
                    else:
                        covered_unverified.add(node.node_type)

    order = {"high": 0, "medium": 1, "low": 2}
    matches.sort(key=lambda m: order.get(m.severity, 99))

    node_type_coverage: dict[str, CoverageStatus] = {}
    for node_type in {n.node_type for n in plan.all_nodes()} - ever_matched:
        if node_type in covered_verified:
            node_type_coverage[node_type] = CoverageStatus.SKILL_CLEARED
        elif node_type in covered_unverified:
            node_type_coverage[node_type] = CoverageStatus.UNVERIFIED
        else:
            node_type_coverage[node_type] = CoverageStatus.NO_APPLICABLE_SKILL

    return DiagnosisResult(
        matches=matches,
        node_type_coverage=node_type_coverage,
        ledger_status=ledger_status,
    )
