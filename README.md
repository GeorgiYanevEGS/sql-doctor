# sql-doctor (MVP)

CLI tool that diagnoses slow PostgreSQL queries by combining:

1. **Deterministic skill matching** — a YAML library of known anti-patterns
   (missing index, implicit type conversion, stale statistics, repeated
   scan inside a loop), matched against the real
   `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` output.
   No LLM involved, zero hallucination risk, works offline.
2. **Grounded LLM fallback** — only triggered if no skill matches. The
   real table schema (columns, indexes, row counts) is read from
   `information_schema` / `pg_catalog` and injected into the prompt, so
   the model never has to guess column names.
3. **Post-LLM validation** — every identifier the LLM mentions is checked
   against the real schema before being shown to the user. If the model
   invents a column, the tool flags it instead of presenting it as fact.
4. **Coverage ledger** — a committed `tests/coverage_ledger.json` that
   records, for every (skill, node type) pair, that a real negative test
   exists: a fixture containing that node type where the skill was proven
   not to fire. When no skill matches a query, the tool distinguishes
   between three outcomes: `SKILL_CLEARED` (a covering skill examined this
   node type and cleared it — backed by a ledger entry), `NO_APPLICABLE_SKILL`
   (no skill claims to cover this node type at all), and `UNVERIFIED` (a
   skill claims coverage but the ledger entry is missing or the ledger
   failed to load). A "No issues found" result means something specific,
   not just "nothing fired."

## Proven results

Tested end-to-end against a real PostgreSQL banking-style schema
(~200k synthetic transaction rows, realistic skew):

| Scenario | Before | After |
|---|---|---|
| `WHERE merchant = 'OMV'` (~10% selectivity, no index) | Seq Scan, **27.8ms** | Bitmap Index Scan, **8.6ms** (after applying the tool's suggested `CREATE INDEX`) — **~3x faster** |
| `WHERE txn_type = 'OPER'` (~60% selectivity) | Correctly identified as *not* needing an index — the planner's Seq Scan choice was already optimal | No false suggestion |
| Correlated subquery (`SELECT ... (SELECT x FROM small_table WHERE ...) ...`) vs. equivalent JOIN | Correlated subquery: **204ms**, `merchants` table scanned **53,425 times** (once per row) | Rewritten as JOIN: **34ms** — planner switches to Hash Join, table scanned once — **~6x faster** |

The second row matters as much as the first: the tool went through two
rounds of real false-positive fixes during testing (a naive `::text`
cast detector, and a selectivity-blind index suggester) before reaching
this behavior. Both are documented as regression tests in `tests/`.

## Why this architecture

Built after real friction with an n8n-based agent that occasionally
hallucinated column names because it had no grounding step. The fix here
is structural, not "better prompting": skills run first (can't
hallucinate — they're pattern matches on facts), anything that does
reach the LLM is checked against ground truth afterwards, and when
nothing fires, the coverage ledger makes "clean" a verifiable claim
rather than a silent pass.

## LLM backend is pluggable on purpose

Some banks (e.g. IT policies limiting staff to Microsoft Copilot /
Azure-hosted models only) can't use arbitrary third-party APIs. The
`core/llm_provider.py` abstraction supports:

- `ollama` — fully local/offline, nothing leaves the network
- `claude` — Anthropic API
- `azure-openai` — customer's own Azure OpenAI Service deployment (the
  Copilot-compatible option for regulated environments)
- `manual` — for the common case where staff have an **M365 Copilot
  chat license but no programmatic API access** (that requires an Entra
  ID app registration and admin approval — not something an individual
  employee can set up). Prints the grounded prompt to the terminal for
  you to paste into Copilot's chat window, then reads the pasted-back
  response and runs it through the same validation as every other
  provider. Zero setup, works with whatever AI access is already
  sanctioned.

Swapping providers never changes the validation logic — grounding and
validation live outside the provider.

## Project layout

```
sql-doctor/
├── cli.py                          # entry point (typer)
├── core/
│   ├── __init__.py                 # makes core a proper package (required for importlib.resources)
│   ├── llm_provider.py             # Ollama / Claude / Azure OpenAI abstraction
│   ├── explain_parser.py           # EXPLAIN JSON -> structured PlanNode tree
│   ├── skill_matcher.py            # loads skills/*.yaml, matches against plan, manages coverage ledger
│   ├── schema_introspect.py        # reads real columns/indexes from the DB
│   └── validator.py                # rejects LLM output referencing unknown names
├── skills/
│   ├── missing_index.yaml
│   ├── implicit_conversion.yaml
│   ├── repeated_seq_scan_in_loop.yaml
│   ├── stale_stats.yaml
│   ├── hash_join_disk_spill.yaml
│   ├── sort_spill_to_disk.yaml
│   ├── redundant_sort_after_ordered_scan.yaml
│   ├── empty_result_bad_estimate.yaml
│   ├── index_only_scan_heap_fetches.yaml
│   ├── nested_loop_bad_plan.yaml
│   ├── parallel_worker_underutilization.yaml
│   ├── repeated_index_scan_in_loop.yaml
│   ├── join_condition_function_wrap.yaml
│   ├── hash_join_build_probe_imbalance.yaml
│   ├── function_scan_bad_estimate.yaml
│   ├── bitmap_heap_lossy.yaml
│   ├── planning_time_dominates.yaml
│   ├── hash_aggregate_disk_spill.yaml
│   ├── subplan_per_row_execution.yaml
│   ├── sort_expression_no_index.yaml
│   ├── unique_without_index.yaml
│   ├── initplan_expensive.yaml
│   ├── merge_join_child_sort_spill.yaml
│   ├── bitmap_or_missing_index_branch.yaml
│   ├── modify_table_seq_scan.yaml
│   ├── append_partition_pruning_failure.yaml
│   ├── unique_sort_noop.yaml
│   ├── cte_scan_single_ref.yaml
│   └── window_agg_sort.yaml
└── tests/
    ├── coverage_helpers.py         # assert_no_match(), VacuousTestError — ledger write contract
    ├── coverage_ledger.json        # committed build artifact — (skill, node_type) negative-test registry
    ├── test_skills.py              # skill-matching regression tests (synthetic EXPLAIN, no DB required)
    ├── test_coverage_ledger.py     # negative tests that populate coverage_ledger.json
    ├── test_coverage_helpers.py    # tests for the ledger helper contract itself
    └── test_integration_ledger.py  # integration test: real ledger authorizes current skills
```

## Try it without a database

```bash
pip install -r requirements.txt
python -m pytest tests/ -v         # runs all 134 tests
python cli.py list-skills          # prints the loaded skill library
```

## Try it against a real PostgreSQL database

```bash
python cli.py analyze \
  --dsn "postgresql://user:pass@localhost:5432/banking_bq" \
  --query "SELECT * FROM transactions WHERE account_id = 42" \
  --llm-provider none
```

Add `--llm-provider ollama` (with a local Ollama server running) or
`--llm-provider azure-openai` (with `AZURE_OPENAI_ENDPOINT`,
`AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_KEY` set) to see the
grounded fallback path when no skill matches.

## Status: MVP, validated against a real database

What's implemented: parser, 30 skills (with selectivity-, loop-, spill-,
child-shape-, low-estimate-, heap-fetch-, outer-child-estimate-, parallel-worker-,
join-condition-, build-probe-imbalance-, function-scan-cardinality-,
bitmap-lossy-page-, planning-time-dominance-, hash-aggregate-disk-spill-,
correlated-subplan-awareness, sort-expression-awareness,
unique-dedup-without-index-awareness, initplan-cost-awareness,
initplan-aggregate-cost-awareness, any-child-spill-awareness,
bitmap-or-branch-awareness, schema-verified-redundant-sort-awareness,
modify-table-unindexed-scan-awareness, partition-pruning-failure-awareness,
unique-sort-noop-awareness, cte-scan-single-reference-awareness, and
window-agg-sort-awareness),
provider abstraction (3 backends), schema introspection, validator, coverage
ledger, CLI wiring, 195 tests:

- **111 skill-matching tests** — synthetic EXPLAIN JSON, no DB required.
  Of these, 6 are regression tests written after real false positives
  were found and fixed during live testing.
- **39 negative tests** — each proves a specific (skill, node type) pair
  doesn't fire on a real negative example; these populate the committed
  coverage ledger.
- **6 coverage-helper tests** — test the ledger write contract itself
  (canonical ordering, VacuousTestError, assertion on skill firing).
- **11 coverage-completeness tests** — assert every (skill, node type)
  pair declared in `covers_node_types` has a corresponding ledger entry;
  closes the gap that regenerate-and-diff cannot catch. Includes a test
  that a bare `covers_node_types: []` without `covers_all_node_types_exempt:
  true` fails by name rather than silently passing.
- **3 CLI tests** — verify `ledger-status` exit codes and output for
  OK, MISSING, and CORRUPT ledger states.
- **4 integration tests** — 1 loads the real committed ledger and confirms
  it authorizes the current skill set; 3 verify `introspect_table()` returns
  correct `partition_key` for partitioned parent, partition child, and
  non-partitioned tables (skip when `SQLDOCTOR_TEST_DSN` not set).
- **1 README meta-test** — asserts this test count matches `pytest
  --collect-only`; prevents the count from silently drifting again.

Historical validation happened against a real database and is captured in
fixed regression tests. CI runs on every push and pull request to main:
the `test` job runs all 89 tests, and the `ledger-integrity` job
regenerates the coverage ledger and diffs against the committed state to
catch a stale ledger before merge.

What's next (not yet done):
- More skills from real-world banking ETL cases (target: ~15-20)
- Packaging as a standalone downloadable binary (PyInstaller). Note:
  requires moving `tests/coverage_ledger.json` to `core/data/` so
  `importlib.resources` can bundle it, and a post-build smoke check to
  confirm the packaged binary finds the ledger at runtime.
- Validator: distinguish "proposed new object" from "hallucinated
  existing object" (see Known limitations below)
- Oracle support is **not a near-term item**. It requires canonicalizing
  node-type names across two dialects (PostgreSQL's `Seq Scan`,
  `Nested Loop`, etc. have no direct Oracle EXPLAIN equivalents),
  redesigning the coverage ledger keys in a way that would be a breaking
  change to the already-shipped PostgreSQL path, and building an entirely
  separate skill library for Oracle-specific anti-patterns. This is a
  separate major version, not an additive task in the current one.

## Known limitations (found during testing)

- **`repeated_seq_scan_in_loop` has only been validated against synthetic
  EXPLAIN JSON**, never against a real PostgreSQL server running a real
  correlated subquery or Nested Loop. Skills that analyze multi-node plan
  shapes are the most likely to hit format assumptions that don't hold in
  practice. Treat findings from this skill as a high-confidence hypothesis
  to verify rather than a confirmed diagnosis until live testing confirms it.

- **`hash_join_disk_spill` has only been validated against synthetic
  EXPLAIN JSON**, never against a real PostgreSQL Hash Join that actually
  spilled to disk. The detection rule (`Hash Batches > Original Hash
  Batches`) is the documented signal, but edge cases (e.g. does the
  planner ever revise Original Hash Batches for reasons unrelated to spill?)
  have not been tested live. Treat as a high-confidence hypothesis until
  confirmed against a real spilling join.

- **`sort_spill_to_disk` detection may not be exhaustive across PostgreSQL
  versions**. The skill fires on `Sort Method == "external merge"`, which
  is the standard disk-sort signal, but whether this is the only value
  a disk-spilling Sort can emit has not been verified against multiple
  PostgreSQL versions or against a real spilling sort — only against the
  single synthetic fixture written during development.

- **`sort_expression_no_index` and `join_condition_function_wrap` share a
  function-name allowlist that doesn't cover all non-indexable expressions**.
  Both skills use the same regex — `\b(lower|upper|to_char|to_number|cast)\s*\(` —
  which misses two broad categories: (1) functions outside the explicit list,
  such as `date_trunc('month', created_at)` (a common banking-reporting sort
  expression); and (2) PostgreSQL's `::type` cast shorthand syntax (e.g.
  `created_at::date`), which produces different text than the `cast(...)` function
  form the regex matches. This is a structural scope limitation, not a bug — the
  skills never claimed to cover these forms, so no ledger entry is needed. Widening
  the regex or switching to a general "any expression that isn't a plain column
  reference" heuristic is the fix, but both carry higher false-positive risk and
  are deferred.

- **`cte_scan_single_ref` fires on recursive CTEs** whose outer CTE Scan is a single
  reference reading many rows — materialization is semantically required for recursive
  CTEs but structurally indistinguishable from a wasteful single-reference case when
  only the plan is available. Verify with `WITH RECURSIVE` keyword in the query text
  before treating the finding as actionable.

- **`unique_sort_noop` suppresses `SELECT DISTINCT col FROM (a UNION ALL b) sub`**
  (outer DISTINCT on a subquery-wrapped UNION ALL) because the Sort→Append shape is
  identical to a plain UNION's implicit dedup. The `sort_child_not_append` guard
  treats both as "probably UNION" — a false negative traded for the more common
  UNION false positive. Inspect manually if this shape appears in a finding-free result.

- **`append_partition_pruning_failure` cannot distinguish a broad-range legitimate
  full-partition scan from a `enable_partition_pruning=off` failure**. Both produce
  an Append node with `Subplans Removed: 0` and a filter referencing the partition key.
  The skill fires on both (the second is correct; the first is a false positive). The
  fix requires inspecting the filter range against the partition boundaries — information
  not present in the EXPLAIN JSON. Treat findings as a hypothesis to verify with
  `SHOW enable_partition_pruning` before acting.

- **Validator can't yet distinguish "hallucinated existing object" from
  "proposed new object name"**. If the LLM suggests `CREATE INDEX
  idx_new_thing ON ...`, the validator correctly flags `idx_new_thing`
  as "not in known schema" — which is technically true but misleading,
  since it's an intentional new-object suggestion, not a hallucination.
  Fix for a later iteration: parse `CREATE INDEX <name>` patterns
  specifically and treat that name as "proposed", not "referenced".

