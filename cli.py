"""
sql-doctor: diagnose slow PostgreSQL queries using a deterministic skill
library first, with an optional, schema-grounded LLM fallback only when
no skill matches.

Usage:
    python cli.py analyze --dsn "postgresql://user:pass@host/db" \
        --query "SELECT * FROM transactions WHERE account_id = 42" \
        --llm-provider ollama
"""

from __future__ import annotations

import json
import sys

import typer

from core.explain_parser import parse_explain_json
from core.llm_provider import LLMError, get_provider
from core.schema_introspect import get_table_row_counts, introspect_query_tables
from core.skill_matcher import load_skills, match_skills
from core.validator import build_grounded_prompt, validate_llm_suggestion

app = typer.Typer(add_completion=False)


def _get_connection(dsn: str):
    import psycopg2

    return psycopg2.connect(dsn)


@app.command()
def analyze(
    dsn: str = typer.Option(..., help="PostgreSQL connection string"),
    query: str = typer.Option(..., help="The slow SQL query to diagnose"),
    llm_provider: str = typer.Option(
        "none",
        help="LLM fallback backend: none | ollama | claude | azure-openai",
    ),
    schema: str = typer.Option("public", help="Postgres schema name"),
):
    """Run EXPLAIN ANALYZE, match deterministic skills, optionally fall back to LLM."""

    conn = _get_connection(dsn)

    typer.echo("Running EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)...")
    with conn.cursor() as cur:
        cur.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {query}")
        raw_explain = cur.fetchone()[0]

    plan = parse_explain_json(raw_explain)
    typer.echo("\n--- Execution plan summary ---")
    typer.echo(plan.summary())

    typer.echo("\n--- Running deterministic skill checks (no LLM) ---")
    skills = load_skills()
    table_row_counts = get_table_row_counts(conn, plan.tables_referenced())
    matches = match_skills(plan, skills, table_row_counts)

    if matches:
        for m in matches:
            typer.secho(f"\n[{m.severity.upper()}] {m.skill_name}", fg=typer.colors.YELLOW, bold=True)
            typer.echo(m.explanation)
            typer.echo(f"Suggested fix:\n{m.fix_template}")
        conn.close()
        return

    typer.echo("No deterministic skill matched.")

    if llm_provider == "none":
        typer.echo(
            "No LLM provider configured — re-run with --llm-provider "
            "ollama|claude|azure-openai for an AI-assisted hypothesis."
        )
        conn.close()
        return

    typer.echo(f"\n--- Falling back to LLM ({llm_provider}), grounded in real schema ---")
    tables = plan.tables_referenced()
    schemas = introspect_query_tables(conn, tables, schema=schema)
    conn.close()

    prompt = build_grounded_prompt(query, plan.summary(), schemas)

    try:
        provider = get_provider(llm_provider)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1)

    if not provider.is_available():
        typer.secho(
            f"Provider '{llm_provider}' is not available/configured "
            "(check credentials or that the local server is running).",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)

    try:
        response = provider.complete(prompt)
    except LLMError as exc:
        typer.secho(f"LLM call failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1)

    validation = validate_llm_suggestion(response.text, schemas)

    typer.echo(f"\n--- LLM hypothesis ({response.provider}/{response.model}) ---")
    typer.echo(response.text.strip())

    if not validation.ok:
        typer.secho(
            f"\n⚠ VALIDATION WARNING: mentions names not found in the real "
            f"schema: {', '.join(validation.unknown_tokens)}. "
            "Treat this suggestion as unverified — do not apply blindly.",
            fg=typer.colors.RED,
            bold=True,
        )
    else:
        typer.secho(
            f"\n✓ All {validation.checked_tokens} referenced identifiers matched "
            "the real schema.",
            fg=typer.colors.GREEN,
        )


@app.command()
def list_skills():
    """Print the loaded deterministic skill library."""
    skills = load_skills()
    for s in skills:
        typer.echo(f"- {s.name} [{s.severity}]: {s.description.strip()}")


if __name__ == "__main__":
    app()
