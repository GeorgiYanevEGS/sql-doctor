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
    # Sort Key array, e.g. ["created_at DESC", "id"]. Used by skills comparing
    # sort order against an underlying index's output order.
    sort_key: list[str] = field(default_factory=list)
    # Index Only Scan only: number of rows that required a heap visit because the
    # visibility map didn't mark the page as all-visible. High values mean the
    # scan degrades toward a regular Index Scan in practice.
    heap_fetches: int | None = None
    # Gather / Gather Merge only: how many parallel workers the plan requested
    # vs. how many the server actually launched. A shortfall means
    # max_worker_processes (or max_parallel_workers) was exhausted at runtime.
    workers_planned: int | None = None
    workers_launched: int | None = None
    # Hash Join / Merge Join only: the join condition text, e.g.
    # "(lower(a.col) = lower(b.col))". Used to detect function wraps on join
    # keys that prevent index use and force less efficient join strategies.
    hash_cond: str | None = None
    merge_cond: str | None = None
    # Bitmap Heap Scan only: populated when work_mem is too small to hold the
    # bitmap exactly, causing PostgreSQL to switch to lossy page-level tracking.
    # Rows on lossy pages must be rechecked against the condition after the heap
    # fetch, so rows_removed_by_recheck reflects wasted heap reads.
    rows_removed_by_recheck: int = 0
    exact_heap_blocks: int | None = None
    lossy_heap_blocks: int | None = None
    # Aggregate node only: Strategy is "Hashed", "Sorted", or "Plain". HashAgg Batches
    # and Disk Usage are populated only on Hashed aggregates that spilled to disk.
    # Disk Usage > 0 means the hash table exceeded work_mem; root cause shared with
    # sort_spill_to_disk (work_mem undersized for the data volume).
    strategy: str | None = None
    hash_agg_batches: int | None = None
    disk_usage_kb: int | None = None
    # Set on child nodes by PostgreSQL to describe the relationship to the parent plan
    # node. Key value: "SubPlan" identifies a correlated subquery node — one that is
    # re-executed once per outer row rather than once per plan.
    parent_relationship: str | None = None
    # Append / MergeAppend only: number of child subplans that runtime partition pruning
    # eliminated before execution. 0 means no pruning occurred despite filter conditions.
    subplans_removed: int | None = None
    children: list["PlanNode"] = field(default_factory=list)

    @property
    def recheck_waste_ratio(self) -> float:
        """Fraction of all heap reads that were wasted on lossy-page rechecks.
        Bounded 0–1; 0.0 when there are no lossy blocks or all rows passed recheck."""
        denom = self.actual_rows + self.rows_removed_by_recheck
        if denom <= 0:
            return 0.0
        return self.rows_removed_by_recheck / denom

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
        sort_key=list(raw.get("Sort Key", [])),
        heap_fetches=int(raw["Heap Fetches"]) if "Heap Fetches" in raw else None,
        workers_planned=int(raw["Workers Planned"]) if "Workers Planned" in raw else None,
        workers_launched=int(raw["Workers Launched"]) if "Workers Launched" in raw else None,
        hash_cond=raw.get("Hash Cond"),
        merge_cond=raw.get("Merge Cond"),
        rows_removed_by_recheck=int(raw.get("Rows Removed by Index Recheck", 0)),
        exact_heap_blocks=int(raw["Exact Heap Blocks"]) if "Exact Heap Blocks" in raw else None,
        lossy_heap_blocks=int(raw["Lossy Heap Blocks"]) if "Lossy Heap Blocks" in raw else None,
        strategy=raw.get("Strategy"),
        hash_agg_batches=int(raw["HashAgg Batches"]) if "HashAgg Batches" in raw else None,
        disk_usage_kb=int(raw["Disk Usage"]) if "Disk Usage" in raw else None,
        parent_relationship=raw.get("Parent Relationship"),
        subplans_removed=int(raw["Subplans Removed"]) if "Subplans Removed" in raw else None,
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
