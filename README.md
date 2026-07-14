# sql-doctor (MVP skeleton)

CLI tool that diagnoses slow PostgreSQL queries by combining:

1. **Deterministic skill matching** — a YAML library of known anti-patterns
   (missing index, implicit type conversion, stale statistics), matched
   against the real `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` output.
   No LLM involved, zero hallucination risk, works offline.
2. **Grounded LLM fallback** — only triggered if no skill matches. The
   real table schema (columns, indexes, row counts) is read from
   `information_schema` / `pg_catalog` and injected into the prompt, so
   the model never has to guess column names.
3. **Post-LLM validation** — every identifier the LLM mentions is checked
   against the real schema before being shown to the user. If the model
   invents a column, the tool flags it instead of presenting it as fact.

## Proven results

Tested end-to-end against a real PostgreSQL banking-style schema
(~200k synthetic transaction rows, realistic skew):

| Scenario | Before | After |
|---|---|---|
| `WHERE merchant = 'OMV'` (~10% selectivity, no index) | Seq Scan, **27.8ms** | Bitmap Index Scan, **8.6ms** (after applying the tool's suggested `CREATE INDEX`) — **~3x faster** |
| `WHERE txn_type = 'OPER'` (~60% selectivity) | Correctly identified as *not* needing an index — the planner's Seq Scan choice was already optimal | No false suggestion |

The second row matters as much as the first: the tool went through two
rounds of real false-positive fixes during testing (a naive `::text`
cast detector, and a selectivity-blind index suggester) before reaching
this behavior. Both are documented as regression tests in `tests/`.

## Why this architecture

Built after real friction with an n8n-based agent that occasionally
hallucinated column names because it had no grounding step. The fix here
is structural, not "better prompting": skills run first (can't
hallucinate — they're pattern matches on facts), and anything that does
reach the LLM is checked against ground truth afterwards.

## LLM backend is pluggable on purpose

Some banks (e.g. IT policies limiting staff to Microsoft Copilot /
Azure-hosted models only) can't use arbitrary third-party APIs. The
`core/llm_provider.py` abstraction supports:

- `ollama` — fully local/offline, nothing leaves the network
- `claude` — Anthropic API
- `azure-openai` — customer's own Azure OpenAI Service deployment (the
  Copilot-compatible option for regulated environments)

Swapping providers never changes the validation logic — grounding and
validation live outside the provider.

## Project layout

```
sql-doctor/
├── cli.py                     # entry point (typer)
├── core/
│   ├── llm_provider.py        # Ollama / Claude / Azure OpenAI abstraction
│   ├── explain_parser.py      # EXPLAIN JSON -> structured PlanNode tree
│   ├── skill_matcher.py       # loads skills/*.yaml, matches against plan
│   ├── schema_introspect.py   # reads real columns/indexes from the DB
│   └── validator.py           # rejects LLM output referencing unknown names
├── skills/
│   ├── missing_index.yaml
│   ├── implicit_conversion.yaml
│   └── stale_stats.yaml
└── tests/
    └── test_skills.py         # synthetic EXPLAIN JSON, no DB required
```

## Try it without a database

```bash
pip install -r requirements.txt
python tests/test_skills.py      # runs 4 synthetic scenarios
python cli.py list-skills        # prints the loaded skill library
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

What's implemented: parser, 3 skills (with selectivity-awareness),
provider abstraction (3 backends), schema introspection, validator, CLI
wiring, 9 tests (all passing) — 4 of which are regression tests written
after real false positives were found and fixed during live testing.

What's next (not yet done):
- More skills from real-world banking ETL cases (target: ~15-20)
- Oracle support (currently PostgreSQL only)
- Packaging as a standalone downloadable binary (PyInstaller)
- Validator: distinguish "proposed new object" from "hallucinated
  existing object" (see Known limitations below)

## Known limitations (found during testing)

- **Validator can't yet distinguish "hallucinated existing object" from
  "proposed new object name"**. If the LLM suggests `CREATE INDEX
  idx_new_thing ON ...`, the validator correctly flags `idx_new_thing`
  as "not in known schema" — which is technically true but misleading,
  since it's an intentional new-object suggestion, not a hallucination.
  Fix for a later iteration: parse `CREATE INDEX <name>` patterns
  specifically and treat that name as "proposed", not "referenced".
