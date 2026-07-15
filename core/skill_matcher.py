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
    matched_node: PlanNode | None


@dataclass
class Skill:
    name: str
    description: str
    detects: dict
    severity: str
    explanation: str
    fix_template: str
    covers_node_types: list[str] = field(default_factory=list)
    # When covers_node_types is [], this field must be True to signal "intentionally not
    # participating in per-node-type completeness checking". A bare [] without this flag
    # is treated as an accidental omission and fails the completeness check by name.
    covers_all_node_types_exempt: bool = False
    scope: str = "node"  # "node" = evaluated per plan node; "plan" = evaluated once per plan
    # Populated by load_skills when a ledger is present; None = no ledger (all trusted).
    _verified_node_types: set[str] | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_yaml_file(cls, path: Path) -> "Skill":
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        detects = data["detects"]
        child_rules = {k for k in detects if k.startswith("child_") and k != "child_node_type"}
        if child_rules and "child_node_type" not in detects:
            raise ValueError(
                f"Skill '{data['name']}' declares {child_rules} but no child_node_type — "
                f"matched_child would always be None, causing silent false negatives."
            )
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            detects=detects,
            severity=data.get("severity", "medium"),
            explanation=data.get("explanation", "").strip(),
            fix_template=data.get("fix_template", "").strip(),
            covers_node_types=data.get("covers_node_types", []),
            covers_all_node_types_exempt=data.get("covers_all_node_types_exempt", False),
            scope=data.get("scope", "node"),
        )

    def covers(self, node_type: str) -> bool:
        return "*" in self.covers_node_types or node_type in self.covers_node_types

    def is_verified_for(self, node_type: str) -> bool:
        """True when coverage of node_type is backed by a ledger negative test (or no ledger loaded)."""
        if self._verified_node_types is None:
            return True
        return node_type in self._verified_node_types

    def matches_node(self, node: PlanNode, table_row_counts: dict[str, int] | None = None, *, execution_time_ms: float = 0.0) -> bool:
        return _evaluate_rules(node, self.detects, table_row_counts, execution_time_ms)

    def matches_plan(self, plan: ParsedPlan) -> bool:
        """Evaluate plan-level detection rules (scope: plan skills only)."""
        rules = self.detects
        if "min_planning_execution_ratio" in rules:
            if plan.execution_time_ms <= 0:
                return False
            if (plan.planning_time_ms / plan.execution_time_ms) < rules["min_planning_execution_ratio"]:
                return False

        # Aggregate InitPlan cost: sum actual_total_time across all InitPlan nodes and
        # compare the total to execution_time_ms. Requires at least aggregate_initplan_min_count
        # nodes so this doesn't overlap with the per-node initplan_expensive skill (count=1).
        if "aggregate_initplan_time_ratio_min" in rules:
            if plan.execution_time_ms <= 0:
                return False
            initplan_nodes = [n for n in plan.all_nodes() if n.parent_relationship == "InitPlan"]
            min_count = rules.get("aggregate_initplan_min_count", 2)
            if len(initplan_nodes) < min_count:
                return False
            total_initplan_time = sum(n.actual_total_time for n in initplan_nodes)
            if total_initplan_time / plan.execution_time_ms < rules["aggregate_initplan_time_ratio_min"]:
                return False

        return True

    def fix_text(self, node: PlanNode | None) -> str:
        if node is None:
            return self.fix_template
        return self.fix_template.format(
            table=node.relation_name or "<table>",
            index=node.index_name or "<index>",
        )


def _evaluate_rules(
    node: PlanNode,
    rules: dict,
    table_row_counts: dict[str, int] | None = None,
    execution_time_ms: float = 0.0,
) -> bool:
    """
    Evaluate a rules dict against a single plan node.

    Called by Skill.matches_node() at the top level and recursively by the
    any_child predicate, which asks the same yes/no question of every immediate
    child and returns True if any one of them satisfies the nested rules.
    """
    table_row_counts = table_row_counts or {}

    if "node_type" in rules:
        allowed = rules["node_type"]
        if isinstance(allowed, list):
            if node.node_type not in allowed:
                return False
        elif node.node_type != allowed:
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
        parts = [node.filter_condition, node.index_condition,
                 node.hash_cond, node.merge_cond]
        if node.sort_key:
            parts.append(" ".join(node.sort_key))
        # For Nested Loop, PostgreSQL has no parent-level join field: the
        # effective join condition appears as Index Cond / Filter on the inner
        # child (children[1]). Extend the haystack with the inner child's
        # conditions so skills can detect function wraps on NL join keys.
        if node.node_type == "Nested Loop" and len(node.children) > 1:
            inner = node.children[1]
            parts.extend([inner.index_condition, inner.filter_condition])
        haystack = " ".join(filter(None, parts))
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

    # String equality check on node.sort_method. Unlike requires_sort_spill (boolean
    # flag hardcoded to "external merge"), this predicate matches any sort_method value
    # and is designed for use inside any_child rules where the target is a Sort child.
    if "sort_method_equals" in rules:
        if node.sort_method != rules["sort_method_equals"]:
            return False

    # Index Only Scan heap fetch ratio: heap_fetches / actual_rows above the
    # threshold means most rows required a heap visit, defeating the scan's
    # purpose. actual_rows == 0 is skipped (ratio undefined).
    if "min_heap_fetch_ratio" in rules:
        if node.heap_fetches is None:
            return False
        if node.actual_rows <= 0:
            return False
        if (node.heap_fetches / node.actual_rows) < rules["min_heap_fetch_ratio"]:
            return False

    # Outer-child row-estimate ratio: evaluates row_estimate_error_ratio on
    # node.children[0] (the outer/left side of a Nested Loop). Guards on
    # plan_rows <= 0 for the same reason as the sibling ratio predicates above.
    if "outer_child_min_row_estimate_error_ratio" in rules:
        if not node.children:
            return False
        outer = node.children[0]
        if outer.plan_rows <= 0:
            return False
        if outer.row_estimate_error_ratio < rules["outer_child_min_row_estimate_error_ratio"]:
            return False

    # Child node type predicate: the node must have at least one immediate child
    # whose node_type appears in the allowed list. Enables parent-looks-down-at-child
    # pattern detection without needing a child-to-parent backreference.
    matched_child: PlanNode | None = None
    if "child_node_type" in rules:
        allowed = rules["child_node_type"]
        matched_child = next(  # first match only — if multiple children satisfy child_node_type, later matches are silently ignored
            (c for c in node.children if c.node_type in allowed), None
        )
        if matched_child is None:
            return False

    # Minimum actual_rows on the companion-predicate matched child (set by child_node_type).
    # Evaluates the matched child's row count — the pre-dedup sort volume for
    # unique_without_index rather than the parent Unique node's output row count.
    if "child_min_actual_rows" in rules:
        if matched_child is None:
            return False
        if matched_child.actual_rows < rules["child_min_actual_rows"]:
            return False

    # Build/probe row count ratio: children[1] is the Hash node (build side),
    # children[0] is the probe side. A high ratio means the planner hashed the
    # larger relation, forcing a bigger in-memory (or spilled) hash table than
    # necessary. Guard on probe_rows <= 0 (empty probe side = no join output,
    # no meaningful imbalance to report).
    if "build_probe_ratio_min" in rules:
        if len(node.children) < 2:
            return False
        probe_rows = node.children[0].actual_rows
        build_rows = node.children[1].actual_rows
        if probe_rows <= 0:
            return False
        if (build_rows / probe_rows) < rules["build_probe_ratio_min"]:
            return False

    # Bitmap Heap Scan lossy-page waste: when work_mem can't hold the exact
    # bitmap, PostgreSQL falls back to lossy page-level tracking; every row on
    # a lossy page must be rechecked, wasting the heap fetch if it doesn't
    # satisfy the condition. recheck_waste_ratio = removed / (actual + removed),
    # bounded 0–1. Guard on denominator via the property itself (returns 0.0).
    if "min_recheck_waste_ratio" in rules:
        if node.recheck_waste_ratio < rules["min_recheck_waste_ratio"]:
            return False

    # HashAggregate disk spill: Disk Usage > threshold KB means the in-memory hash
    # table exceeded work_mem and PostgreSQL wrote batches to disk. Uses strictly-
    # greater-than (threshold 0 = any spill at all); Disk Usage is 0 for fully
    # in-memory aggregation. Root cause shared with requires_sort_spill: work_mem
    # undersized for the data volume.
    if "min_disk_usage_kb" in rules:
        if node.disk_usage_kb is None:
            return False
        if node.disk_usage_kb <= rules["min_disk_usage_kb"]:
            return False

    # Parallel worker shortfall: workers_launched < workers_planned means the
    # server couldn't provide all requested workers at execution time —
    # typically max_worker_processes or max_parallel_workers exhaustion.
    if rules.get("requires_worker_shortfall"):
        if node.workers_planned is None or node.workers_launched is None:
            return False
        if node.workers_launched >= node.workers_planned:
            return False

    # Correlated subplan: the node is the root of a SubPlan re-executed once per
    # outer row. "SubPlan" distinguishes from Nested Loop inner children ("Inner")
    # and Append members ("Member") — all of which also have high actual_loops but
    # different root causes and different fixes.
    if rules.get("requires_subplan_parent"):
        if node.parent_relationship != "SubPlan":
            return False

    # Non-correlated (init) subplan: executed exactly once before the main plan.
    # "InitPlan" distinguishes from "SubPlan" (correlated, re-executed per outer row).
    if rules.get("requires_initplan_parent"):
        if node.parent_relationship != "InitPlan":
            return False

    # InitPlan time ratio: the node's own actual_total_time as a fraction of the
    # overall plan execution time. Guards on execution_time_ms <= 0 (ratio undefined
    # — conservative: don't fire if plan time is missing or zero).
    if "initplan_time_ratio_min" in rules:
        if execution_time_ms <= 0:
            return False
        if node.actual_total_time / execution_time_ms < rules["initplan_time_ratio_min"]:
            return False

    # any_child: fires if at least one immediate child satisfies the nested rules dict.
    # Evaluates _evaluate_rules() independently against every child — an existential
    # quantifier across children, not first-match-and-inspect like child_node_type.
    # Placed last so node-level gates (node_type, etc.) eliminate the parent early
    # before the child scan runs.
    if "any_child" in rules:
        child_rules = rules["any_child"]
        if not any(
            _evaluate_rules(c, child_rules, table_row_counts, execution_time_ms)
            for c in node.children
        ):
            return False

    return True


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

    # Plan-level skills: evaluated once per plan, not per node.
    for skill in skills:
        if skill.scope == "plan" and skill.matches_plan(plan):
            matches.append(
                SkillMatch(
                    skill_name=skill.name,
                    severity=skill.severity,
                    explanation=skill.explanation,
                    fix_template=skill.fix_text(None),
                    matched_node=None,
                )
            )

    # Node-level skills: evaluated per node (existing loop).
    for node in plan.all_nodes():
        node_matched = False
        for skill in skills:
            if skill.scope == "plan":
                continue
            if skill.matches_node(node, table_row_counts, execution_time_ms=plan.execution_time_ms):
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
                if skill.scope == "plan":
                    continue
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
