"""
Parses PostgreSQL `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` output into a
flat, easy-to-scan structure that skills can pattern-match against without
each skill needing to know how to walk a nested plan tree.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PlanNode:
    node_type: str
    relation_name: str | None
    index_name: str | None
    filter_condition: str | None
    index_condition: str | None
    plan_rows: float
    actual_rows: float
    total_cost: float
    actual_total_time: float
    actual_loops: int = 1
    # Hash node only: populated when Node Type == "Hash". Batches > original_hash_batches
    # means the build side grew beyond work_mem and spilled to disk mid-execution.
    hash_batches: int | None = None
    original_hash_batches: int | None = None
    # Sort node only: populated when Node Type == "Sort". "external merge" means the
    # sort exceeded work_mem and wrote sorted runs to disk.
    sort_method: str | None = None
    sort_space_type: str | None = None
    children: list["PlanNode"] = field(default_factory=list)

    @property
    def row_estimate_error_ratio(self) -> float:
        """
        How wrong the planner's row estimate was. >10x in either direction
        is the classic signal for stale statistics.
        """
        if self.plan_rows <= 0:
            return 0.0
        return self.actual_rows / self.plan_rows

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass
class ParsedPlan:
    root: PlanNode
    execution_time_ms: float
    planning_time_ms: float

    def all_nodes(self) -> list[PlanNode]:
        return list(self.root.walk())

    def tables_referenced(self) -> list[str]:
        return sorted({n.relation_name for n in self.all_nodes() if n.relation_name})

    def summary(self) -> str:
        """Short text summary used both for human display and LLM grounding."""
        lines = [
            f"Planning time: {self.planning_time_ms:.2f} ms, "
            f"Execution time: {self.execution_time_ms:.2f} ms"
        ]
        for node in self.all_nodes():
            rel = f" on {node.relation_name}" if node.relation_name else ""
            idx = f" using {node.index_name}" if node.index_name else ""
            loops = f", loops={node.actual_loops}" if node.actual_loops > 1 else ""
            lines.append(
                f"- {node.node_type}{rel}{idx}: "
                f"est. {node.plan_rows:.0f} rows, actual {node.actual_rows:.0f} rows, "
                f"cost={node.total_cost:.1f}, time={node.actual_total_time:.2f}ms{loops}"
            )
        return "\n".join(lines)


def _parse_node(raw: dict) -> PlanNode:
    node = PlanNode(
        node_type=raw.get("Node Type", "Unknown"),
        relation_name=raw.get("Relation Name"),
        index_name=raw.get("Index Name"),
        filter_condition=raw.get("Filter"),
        index_condition=raw.get("Index Cond"),
        plan_rows=float(raw.get("Plan Rows", 0)),
        actual_rows=float(raw.get("Actual Rows", 0)),
        total_cost=float(raw.get("Total Cost", 0)),
        actual_total_time=float(raw.get("Actual Total Time", 0)),
        actual_loops=int(raw.get("Actual Loops", 1)),
        hash_batches=int(raw["Hash Batches"]) if "Hash Batches" in raw else None,
        original_hash_batches=int(raw["Original Hash Batches"]) if "Original Hash Batches" in raw else None,
        sort_method=raw.get("Sort Method"),
        sort_space_type=raw.get("Sort Space Type"),
    )
    for child_raw in raw.get("Plans", []):
        node.children.append(_parse_node(child_raw))
    return node


def parse_explain_json(explain_output: list | dict) -> ParsedPlan:
    """
    explain_output: the parsed JSON (already json.loads'd) returned by
    `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) <query>`. psycopg2 returns
    this as a list containing one dict.
    """
    if isinstance(explain_output, list):
        payload = explain_output[0]
    else:
        payload = explain_output

    root = _parse_node(payload["Plan"])
    return ParsedPlan(
        root=root,
        execution_time_ms=float(payload.get("Execution Time", 0)),
        planning_time_ms=float(payload.get("Planning Time", 0)),
    )
