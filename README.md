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
│   └── stale_stats.yaml
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
python -m pytest tests/ -v         # runs all 26 tests
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

What's implemented: parser, 4 skills (with selectivity- and
loop-awareness), provider abstraction (3 backends), schema
introspection, validator, coverage ledger, CLI wiring, 26 tests:

- **13 skill-matching tests** — synthetic EXPLAIN JSON, no DB required.
  Of these, 6 are regression tests written after real false positives
  were found and fixed during live testing.
- **6 negative tests** — each proves a specific (skill, node type) pair
  doesn't fire on a real negative example; these populate the committed
  coverage ledger.
- **6 coverage-helper tests** — test the ledger write contract itself
  (canonical ordering, VacuousTestError, assertion on skill firing).
- **1 integration test** — loads the real committed ledger and confirms
  it authorizes the current skill set without errors.

Historical validation happened against a real database and is captured in
fixed regression tests. Continuous enforcement of the coverage guarantee
on every future change is designed but not yet built: the CI pipeline
does not yet include a regenerate-and-diff step (`git diff --exit-code
tests/coverage_ledger.json` after re-running the negative tests) to
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

- **Validator can't yet distinguish "hallucinated existing object" from
  "proposed new object name"**. If the LLM suggests `CREATE INDEX
  idx_new_thing ON ...`, the validator correctly flags `idx_new_thing`
  as "not in known schema" — which is technically true but misleading,
  since it's an intentional new-object suggestion, not a hallucination.
  Fix for a later iteration: parse `CREATE INDEX <name>` patterns
  specifically and treat that name as "proposed", not "referenced".

- **Coverage ledger CI enforcement is not yet built**. The committed
  `tests/coverage_ledger.json` is authoritative but only verified
  manually — no CI step regenerates it and diffs to catch a stale
  ledger before merge.
