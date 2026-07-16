# CONTRIBUTING — Adding a new skill to sql-doctor

## Who this is for

You have a PostgreSQL anti-pattern you've been burned by in production and you want
to encode it so the tool catches it automatically. You know SQL and PostgreSQL well.
You can read Python but you don't need to write any — every step below shows you
exactly what to type.

---

## What a skill is

A skill is a single YAML file in `skills/`. It describes a pattern to look for in a
`EXPLAIN (ANALYZE, FORMAT JSON)` plan and what to tell the user if the pattern is
found. The tool reads every YAML in that directory on startup — adding a file is
enough to register a new skill.

---

## Step 1: Write the skill YAML

Create a file in `skills/` named after your skill, e.g. `skills/my_skill.yaml`.
Below is `missing_index.yaml` — the simplest shipped skill — annotated line by line.

```yaml
# The internal name. Used in test assertions and in the tool's output.
# Must be unique across all files in skills/.
name: missing_index

# Which EXPLAIN node type(s) this skill monitors.
# The ledger system (explained in Step 4) uses this to track that the skill
# has been proven not to fire on a real negative example of each type.
# Must match the node_type in the detects block below.
covers_node_types: ["Seq Scan"]

# One-line summary shown by `sql-doctor list-skills`.
description: >
  A sequential scan is filtering a large table by a condition that could
  be served by an index instead. This is the single most common cause of
  slow OLTP-style queries.

# The matching rules. ALL conditions listed here must be true for the skill
# to fire. If any condition fails, the skill stays silent.
detects:
  # Which EXPLAIN node type to look for.
  node_type: "Seq Scan"

  # Only fire if this node didn't use an index.
  # (If an index was used, the node type would be "Index Scan" anyway,
  # but this guard catches edge cases the type check alone might miss.)
  requires_no_index: true

  # Don't flag small tables — a Seq Scan on 50 rows is fast enough.
  # actual_rows is what PostgreSQL reported it actually returned.
  min_actual_rows: 1000

  # Don't flag when the query returns most of the table. A query that
  # matches 60% of all rows is CORRECT to scan sequentially — random
  # I/O for that many rows would be slower than one sequential pass.
  # (This threshold was found by testing against real data: a 60%-
  # selectivity filter kept getting flagged even after adding the index,
  # because the planner correctly refused to use it.)
  max_selectivity_ratio: 0.25

# How urgently to flag this. Options: low, medium, high.
severity: high

# What the user reads when this skill fires. Plain prose.
explanation: >
  The planner performed a full sequential scan over a table with a filter
  condition, and no index was used. On a table this size, a targeted index
  would let PostgreSQL jump directly to matching rows instead of reading
  every row.

# The suggested fix shown to the user. Plain prose or a SQL snippet.
fix_template: >
  Consider adding an index on the column(s) used in the WHERE clause of
  the "{table}" scan, e.g.:
    CREATE INDEX CONCURRENTLY idx_{table}_filter ON {table} (<filter_column>);
  Verify with EXPLAIN ANALYZE afterwards that the planner switches to an
  Index Scan / Bitmap Index Scan.
```

### Core predicates

These five cover the large majority of new anti-patterns a skill author is likely
to encode. Start here; see the [Advanced predicates](#advanced-predicates) section
only if your pattern genuinely requires something these can't express.

| Predicate | Type | What it checks |
|---|---|---|
| `node_type` | string (required) | Which EXPLAIN node type to match. Common values: `"Seq Scan"`, `"Sort"`, `"Nested Loop"`, `"Hash Join"`, `"Aggregate"`, `"WindowAgg"`, `"Unique"`, `"CTE Scan"`. Must match `covers_node_types`. |
| `min_actual_rows` | integer | The matched node's actual row count must be at least this. Use to avoid flagging small tables. |
| `child_node_type` | list of strings | The matched node must have a direct child whose type is one of these. E.g. `["Sort"]` to detect Sort inside a WindowAgg. Sets `matched_child` for the predicate below. |
| `child_min_actual_rows` | integer | Use with `child_node_type`. The matched child's actual row count must be at least this. |
| `condition_pattern` | regex string | The matched node's filter or join condition must match this regex. E.g. `"~~ '%"` to detect a LIKE with a leading wildcard. |

---

## Step 2: Verify against a real query

**Do this before writing tests.** Several skills in this codebase had wrong field
names or thresholds that only became visible against real PostgreSQL output —
the synthetic test fixture looked right but didn't match what the server actually
produces. Catching that now is cheaper than chasing it later.

If you have a database connection:

```bash
python cli.py analyze \
  --dsn "postgresql://user:pass@localhost:5432/mydb" \
  --query "SELECT * FROM transactions WHERE status = 'PENDING'" \
  --llm-provider none
```

If your new skill fires, it appears in the output. If it doesn't fire when you
expected it to, capture the raw EXPLAIN JSON to see what the plan actually looks
like:

```sql
-- Run this in psql or any SQL client connected to your database:
EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
SELECT * FROM transactions WHERE status = 'PENDING';
```

Copy the JSON output. The field names in your `detects` block must match exactly
what PostgreSQL puts there — `"Actual Rows"`, not `"actual_rows"`; `"Node Type"`,
not `"node_type"`. The parser handles the normalisation, but your `condition_pattern`
regex runs against the raw condition text as PostgreSQL writes it.

---

## Step 3: Add a positive test

A positive test proves the skill fires when it should. Add it to `tests/test_skills.py`.

```python
def test_my_skill_fires():
    # Build a minimal synthetic EXPLAIN fixture.
    # Use the real JSON from Step 2 as your guide — copy the fields your
    # skill actually checks, discard the rest.
    explain_json = [{
        "Plan": {
            "Node Type": "Seq Scan",           # must match detects.node_type
            "Relation Name": "transactions",
            "Filter": "(status = 'PENDING')",
            "Plan Rows": 1,
            "Actual Rows": 50000,              # above min_actual_rows: 1000
            "Total Cost": 45000.0,
            "Actual Total Time": 320.5,
        },
        "Planning Time": 0.3,
        "Execution Time": 321.0,
    }]

    # Turn the JSON into a parsed plan tree.
    plan = parse_explain_json(explain_json)

    # Run every skill against the plan.
    result = match_skills(
        plan,
        SKILLS,
        table_row_counts={"transactions": 500000},  # needed by selectivity predicates
        ledger_status=LedgerStatus.OK,
    )

    # Collect the names of every skill that fired.
    names = {m.skill_name for m in result.matches}

    # Assert yours is among them.
    assert "my_skill" in names, f"expected my_skill to fire, got {names}"
```

Run it immediately to confirm it passes:

```bash
python -m pytest tests/test_skills.py::test_my_skill_fires -v
```

---

## Step 4: Add a negative test and update the ledger

**This step is required.** The tool distinguishes between "a skill examined this
node type and cleared it" and "no skill looked at this node type at all." That
distinction only holds if every skill has a proven negative example — a plan
containing the relevant node type where the skill genuinely did not fire.

CI rejects any skill declared in `covers_node_types` that lacks a matching ledger
entry. The ledger entry is created automatically when your negative test runs.

Add the negative test to `tests/test_coverage_ledger.py`:

```python
def test_negative_my_skill_small_table():
    # Use assert_no_match instead of a plain assert.
    # It does two things: checks the skill didn't fire, AND writes the
    # (skill_name, node_type) pair to tests/coverage_ledger.json so CI
    # knows this combination has a real negative example behind it.
    assert_no_match(
        "my_skill",     # skill name — must match the name: field in your YAML
        "Seq Scan",     # node type — must match covers_node_types in your YAML
        [{
            "Plan": {
                "Node Type": "Seq Scan",       # must be present — the test is
                "Relation Name": "tiny_table", # vacuous without it
                "Filter": "(status = 'X')",
                "Plan Rows": 5,
                "Actual Rows": 5,              # below min_actual_rows: 1000
                "Total Cost": 1.0,             # skill must NOT fire on this
                "Actual Total Time": 0.05,
            },
            "Planning Time": 0.1,
            "Execution Time": 0.15,
        }],
        SKILLS,
    )
```

**The plan must actually contain the node type you claim** (`"Seq Scan"` above).
If it doesn't, `assert_no_match` raises `VacuousTestError` — a test that proves
nothing is worse than no test at all.

Now regenerate the ledger and commit it:

```bash
# Regenerate coverage_ledger.json (runs all ledger tests, updates the file):
python -m pytest tests/test_coverage_ledger.py

# Commit the updated ledger alongside your new skill:
git add tests/coverage_ledger.json
```

---

## Step 5: Run the full suite

```bash
python -m pytest tests/ -q
```

All tests should pass. If the completeness check fails, it means `covers_node_types`
in your YAML names a node type that has no ledger entry yet — add another negative
test for that type and regenerate.

---

## Advanced predicates

You'll rarely need these. If your pattern seems to require one, it's worth checking
whether the skill can be simplified first — some of these predicates interact with
each other in non-obvious ways, and a skill that needs three advanced predicates to
avoid false positives is often a signal the detection logic belongs in Python rather
than YAML.

| Predicate | What it does |
|---|---|
| `requires_no_index: true` | Only fire if the matched node has no associated index (i.e. `node.index_name` is falsy). Useful when the node type alone doesn't guarantee absence of an index. Note: this is a plan-field check only — it does not query the schema catalog. See `seq_scan_filter_column_unindexed` below if you need a schema-verified version. |
| `any_descendant: true` | Fire when ANY descendant node (not just the direct child) matches `node_type`. The search goes up to 5 levels deep by default; add `any_descendant_max_depth: 3` to limit it. Used by `modify_table_seq_scan` to find a Seq Scan anywhere inside an UPDATE/DELETE plan. |
| `requires_schema_context: true` | The skill abstains (produces no finding) when run without a live database connection. Use when your check needs to know index definitions or partition keys. Skills with this flag are exempt from the ledger completeness check when run offline. |
| `parent_relationship_exclude: ["Inner"]` | Do not fire when the matched node's `Parent Relationship` field is one of the listed values. Used by `unique_sort_noop` to avoid misfiring on the inner side of a Merge Join. |
| `sort_child_not_append: true` | Block the skill when the matched child's first grandchild is an Append node. Used to distinguish `SELECT DISTINCT` (Sort→Scan) from `UNION` implicit dedup (Sort→Append→Scans). |
| `min_children: 4` | The matched node must have at least this many direct children. Used by `append_partition_pruning_failure` to avoid flagging two-partition tables. |
| `max_pruning_ratio: 0.1` | For Append nodes: the fraction of subplans removed by partition pruning must be ≤ this. A high ratio means pruning worked; a low ratio means it didn't. |
| `partition_key_filter_intersects: true` | Requires schema context. Fires only when the filter columns on child scans overlap the table's partition key columns. |
| `seq_scan_filter_column_unindexed: true` | Requires schema context. Fires only when the Seq Scan's filter column has no index on it. More precise than `requires_no_index` but needs a database connection. |
| `max_cte_reference_count: 1` | For CTE Scan nodes: fire only when the CTE was referenced at most this many times in the plan. |

---

## Extending the matcher with a new predicate (Python)

Most new anti-patterns need only a new YAML file. This section is for the less
common case where no existing predicate can express your rule and you need to add
one in Python.

### When YAML is enough vs. when you need Python

YAML is enough when your rule can be expressed as:
- A node type check
- A row-count threshold on the matched node or its direct child
- A regex match against the node's filter/condition text
- Whether the node has an index attached

You need Python when your rule requires:
- Comparing values across multiple nodes simultaneously (e.g., checking that a
  Sort key is a left-prefix of the child scan's index columns)
- Querying schema catalog information not present in the plan JSON (index
  definitions, partition keys, column types)
- Computing a ratio or threshold from multiple plan fields that no single field
  captures on its own

### Where predicates live

All predicate evaluation happens in `_evaluate_rules()` in
`core/skill_matcher.py`. The function receives the parsed `detects:` block as a
`rules` dict and a `PlanNode`, and returns `True` only if every rule in the dict
passes.

The pattern is a standalone `if` guard per predicate that returns `False` early:

```python
# From _evaluate_rules() — the complete pattern for a new predicate:
if "min_actual_rows" in rules and node.actual_rows < rules["min_actual_rows"]:
    return False
```

To add a new predicate:

1. Pick a YAML key name (e.g., `requires_no_parent_loops`).
2. Add the guard block inside `_evaluate_rules()` at the point where it logically
   belongs — simpler, cheaper checks go earlier so they eliminate nodes before
   more expensive checks run.
3. Write a skill YAML that uses the new key, plus a positive and negative test.
4. Add the ledger entry (Step 4 above).

There is no dispatch table, no registry, no inheritance — the function is a
sequential list of guards. Reading 60 lines of `_evaluate_rules()` gives you the
complete predicate vocabulary.

### The pre-implementation grilling

Before writing any YAML or Python for a new skill or predicate, the project uses
a structured 6-question grilling session to surface design decisions that are much
harder to reverse after implementation. The questions cover: the detection shape,
threshold justification, which node types are involved, schema-context dependency,
false-positive cases, and the best negative example.

Evidence from the git log of what gets caught — and what slips through when it
doesn't happen:

- **`6cb93a9`** — "Refactor: extract `_evaluate_rules()`; add `any_child` +
  `sort_method_equals`; skill: `merge_join_child_sort_spill`" — grilling the
  `merge_join_child_sort_spill` skill revealed it needed to inspect the sort
  child's method string generically, not just test for `"external merge"`. That
  gap drove the `sort_method_equals` predicate and a refactor to isolate each
  predicate as its own guard, all done before the skill itself was committed.

- **`1af2059`** — "bitmap_or_missing_index_branch v2: suppress indexed-column
  false positives" — the `v2` exists because the original implementation was
  committed without grilling and fired on BitmapOr nodes where the column
  already had an index the planner chose not to use. The false-positive scenario
  would have surfaced in grilling question 5 ("describe the best negative
  example").

- **`865eaa5`** — "redundant_sort_after_ordered_scan v2: schema-verified, no
  more shape-only false positives" — shape-only detection (no schema context)
  fired whenever a Sort happened to sit above a scan, even when no index existed
  to replace it. Adding `requires_schema_context: true` and verifying the index
  is the fix; grilling question 4 ("does this need schema context?") would have
  caught it before v1 was merged.

To run a grilling session: tell the assistant `grill me on [skill name]` and
answer the 6 questions before writing any code.

### Running the full suite

```bash
# Full suite — must pass before any commit:
python -m pytest tests/ -q

# Ledger only — regenerates tests/coverage_ledger.json in place:
python -m pytest tests/test_coverage_ledger.py

# Single test during active development:
python -m pytest tests/test_skills.py::test_my_skill_fires -v
```

The coverage ledger (`tests/coverage_ledger.json`) is rebuilt from scratch on
every `pytest tests/test_coverage_ledger.py` run: each `assert_no_match()` call
appends its `(skill_name, node_type)` pair, and a completeness check at the end
verifies that every `covers_node_types` declaration in every YAML has at least
one corresponding entry. If it doesn't, the test suite fails. The ledger is
committed to source control so CI can run the completeness check without
re-executing the ledger tests against a live database.
