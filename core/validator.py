"""
Post-LLM validation layer.

The rule: an LLM suggestion is never shown to the user unless every
column / index / table name it mentions actually exists in the schema we
introspected. This is what turns "the model probably knows the schema"
into "we mathematically checked it".

This is intentionally conservative — false rejections (a good suggestion
gets blocked because of a phrasing quirk) are far cheaper than false
acceptances (a hallucinated column reaches a production database admin).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.schema_introspect import TableSchema

# DB identifiers in real schemas are almost always snake_case:
# account_id, transaction_date, idx_foo_bar. Restricting candidates to
# tokens containing an underscore is a deliberate precision/recall
# trade-off: it misses single-word column names (a column literally
# called "amount"), but it eliminates the much more common failure mode
# of flagging ordinary English prose words ("also", "caused", "field")
# as if they were hallucinated schema objects. A quoted identifier
# ("Some Column") is also accepted since quoting is an explicit signal
# of "this is a name", not prose.
_SNAKE_CASE_RE = re.compile(r"\b[a-zA-Z_][a-zA-Z0-9]*(?:_[a-zA-Z0-9]+)+\b")
_QUOTED_RE = re.compile(r'"([^"]+)"')

_SQL_PHRASE_KEYWORDS = {
    "group_by", "order_by", "left_join", "inner_join", "right_join",
    "primary_key", "foreign_key", "not_null",
}


@dataclass
class ValidationResult:
    ok: bool
    unknown_tokens: list[str]
    checked_tokens: int


def _extract_candidate_identifiers(text: str) -> set[str]:
    candidates: set[str] = set()

    for match in _SNAKE_CASE_RE.finditer(text):
        token = match.group(0).lower()
        if token in _SQL_PHRASE_KEYWORDS:
            continue
        candidates.add(token)

    for match in _QUOTED_RE.finditer(text):
        token = match.group(1).strip().lower()
        if token:
            candidates.add(token)

    return candidates


def validate_llm_suggestion(llm_text: str, schemas: dict[str, TableSchema]) -> ValidationResult:
    """
    Checks every plausible identifier-looking token in the LLM's answer
    against the known set of real table/column/index names.

    This is a heuristic, not a SQL parser — by design. A full parser would
    be more precise but also more brittle against LLM prose that mixes
    explanation with SQL snippets. The heuristic errs toward flagging too
    much rather than missing a hallucination.
    """
    known: set[str] = set()
    for schema in schemas.values():
        known.add(schema.table_name.lower())
        known |= {c.lower() for c in schema.column_names}
        known |= {i.lower() for i in schema.index_names}

    candidates = _extract_candidate_identifiers(llm_text)
    unknown = sorted(c for c in candidates if c not in known)

    return ValidationResult(ok=not unknown, unknown_tokens=unknown, checked_tokens=len(candidates))


def build_grounded_prompt(query: str, plan_summary: str, schemas: dict[str, TableSchema]) -> str:
    """
    Assembles the prompt sent to the LLM. Every fact about the database
    (columns, indexes, row counts) is injected explicitly — the model is
    never asked to "recall" schema from training data.
    """
    schema_blocks = "\n\n".join(s.as_prompt_block() for s in schemas.values())

    return f"""You are a PostgreSQL performance expert. You will be given:
1. A SQL query
2. Its actual EXPLAIN ANALYZE output (summarized)
3. The REAL schema of every table involved — do not assume any column,
   index, or table exists beyond what is listed below.

IMPORTANT: Only reference column/table/index names that appear explicitly
in the "Known schema" section. If you are not sure a name is correct, say
so instead of guessing.

## Query
{query}

## Execution plan summary
{plan_summary}

## Known schema (ground truth — do not invent names outside this list)
{schema_blocks}

## Task
In 3-5 sentences, diagnose the most likely performance issue and propose
one concrete fix (e.g. an index to add, expressed as valid SQL using only
the columns listed above).
"""
