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

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import typer

from core.explain_parser import ParsedPlan, parse_explain_json
from core.llm_provider import LLMError, LLMResponse, get_provider
from core.schema_introspect import get_table_row_counts, introspect_query_tables
from core.skill_matcher import (
    CoverageStatus,
    DEFAULT_LEDGER_PATH,
    DiagnosisResult,
    LedgerStatus,
    SkillMatch,
    load_skills,
    match_skills,
)
from core.validator import ValidationResult, build_grounded_prompt, validate_llm_suggestion

app = typer.Typer(add_completion=False)


@dataclass
class LLMOutcome:
    """
    Result of the optional schema-grounded LLM fallback. Populated by
    run_analysis() only when a provider is selected and no deterministic
    skill matched. All fields default to empty so the common (no-LLM) path
    leaves it inert.

    response   : the raw LLM completion (text/provider/model), or None.
    validation : identifier-grounding check of response.text, or None when
                 there was no text to validate (e.g. manual --quick mode).
    error      : human-readable failure reason (unknown/unavailable provider
                 or a failed call), or None on success. Callers decide how to
                 surface it (CLI exits non-zero; GUI shows a red panel).
    attempted  : True if the fallback path ran at all (provider selected and
                 no skill matched), regardless of success.
    """
    response: LLMResponse | None = None
    validation: ValidationResult | None = None
    error: str | None = None
    attempted: bool = False


@dataclass
class AnalysisResult:
    """
    Return type of run_analysis(). Bundles everything the caller needs:
    the deterministic diagnosis, the parsed plan (for tree rendering), the
    schema context (for LLM prompt building or display), and the optional
    LLM fallback outcome.
    """
    diagnosis: DiagnosisResult
    plan: ParsedPlan
    schemas: dict
    llm: LLMOutcome = field(default_factory=LLMOutcome)

    # Mirror DiagnosisResult's interface so callers can use result.matches,
    # result.node_type_coverage, etc. without unpacking.
    @property
    def matches(self) -> list[SkillMatch]:
        return self.diagnosis.matches

    @property
    def node_type_coverage(self) -> dict:
        return self.diagnosis.node_type_coverage

    @property
    def ledger_status(self) -> LedgerStatus:
        return self.diagnosis.ledger_status

    @property
    def ledger_load_error(self) -> bool:
        return self.diagnosis.ledger_load_error


def _get_connection(dsn: str):
    import psycopg2
    return psycopg2.connect(dsn)


def _build_provider_kwargs(
    llm_provider: str, llm_model: str | None, llm_host: str | None, quick: bool
) -> dict:
    """
    Map the flat CLI/GUI options onto each provider's constructor kwargs.

    - azure-openai: a model name means the deployment name.
    - manual: forwards the --quick flag.
    - ollama/claude: a model name means the model; ollama also accepts a host.
    Any option left unset falls back to the provider's own default (env vars).
    """
    if llm_provider == "azure-openai" and llm_model:
        return {"deployment": llm_model}
    if llm_provider == "manual":
        return {"quick": quick}
    kwargs: dict = {}
    if llm_model:
        kwargs["model"] = llm_model
    if llm_provider == "ollama" and llm_host:
        kwargs["host"] = llm_host
    return kwargs


def _invoke_llm_fallback(
    llm_provider: str,
    llm_model: str | None,
    llm_host: str | None,
    quick: bool,
    query: str,
    plan: ParsedPlan,
    schemas: dict,
) -> LLMOutcome:
    """
    Run the schema-grounded LLM fallback and return an LLMOutcome.

    Never raises for expected failure modes (unknown/unavailable provider, a
    failed call) — those are captured in outcome.error so both the CLI and the
    GUI can decide how to surface them. Shared single source of truth for the
    provider call + post-LLM validation.
    """
    prompt = build_grounded_prompt(query, plan.summary(), schemas)
    try:
        provider = get_provider(
            llm_provider, **_build_provider_kwargs(llm_provider, llm_model, llm_host, quick)
        )
    except ValueError as exc:
        return LLMOutcome(error=str(exc), attempted=True)

    if not provider.is_available():
        return LLMOutcome(
            error=(
                f"Provider '{llm_provider}' is not available/configured "
                "(check credentials or that the local server is running)."
            ),
            attempted=True,
        )

    try:
        response = provider.complete(prompt)
    except LLMError as exc:
        return LLMOutcome(error=f"LLM call failed: {exc}", attempted=True)

    # Empty text (e.g. manual --quick) has nothing to validate.
    validation = validate_llm_suggestion(response.text, schemas) if response.text else None
    return LLMOutcome(response=response, validation=validation, attempted=True)


def run_analysis(
    dsn: str,
    query: str,
    llm_provider: str = "none",
    llm_model: str | None = None,
    llm_host: str | None = None,
    schema: str = "public",
    quick: bool = False,
    on_status: Callable[[str], None] | None = None,
) -> AnalysisResult:
    """
    Core analysis pipeline, callable from both the CLI and the GUI.

    on_status: optional callback invoked with a human-readable status string
    at each pipeline stage. The GUI uses this to drive its progress indicator;
    the CLI passes typer.echo. When None, no status output is produced.
    """
    def _status(msg: str) -> None:
        if on_status:
            on_status(msg)

    conn = _get_connection(dsn)

    _status("Running EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)...")
    with conn.cursor() as cur:
        cur.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {query}")
        raw_explain = cur.fetchone()[0]

    plan = parse_explain_json(raw_explain)

    _status("\n--- Running deterministic skill checks (no LLM) ---")
    loaded = load_skills(ledger_path=DEFAULT_LEDGER_PATH)
    table_row_counts = get_table_row_counts(conn, plan.tables_referenced())

    # Schema introspection runs unconditionally — required for schema-dependent skill
    # predicates (e.g. requires_schema_context). This adds one extra DB round-trip
    # (3 catalog queries per referenced table: columns, indexes, row estimate) on every
    # analyze call, regardless of whether a skill match is found or the LLM fallback is
    # triggered. Cost: low for catalog queries (~1–5ms per table on a warm server), but
    # non-zero — measure with `time cli.py analyze ...` against your own instance to
    # quantify before deploying to high-frequency callers.
    _status("Introspecting table schemas for schema-dependent skill checks...")
    schemas = introspect_query_tables(conn, plan.tables_referenced(), schema=schema)
    conn.close()

    diagnosis = match_skills(
        plan,
        loaded.skills,
        table_row_counts,
        ledger_status=loaded.ledger_status,
        schema_context=schemas,
    )

    # Schema-grounded LLM fallback: only when a provider is selected, no
    # deterministic skill fired, AND the plan has genuine uncertainty — at least
    # one NO_APPLICABLE_SKILL or UNVERIFIED node. A fully SKILL_CLEARED plan is a
    # ledger-backed proven-clean result; we do not second-guess it with an LLM.
    llm = LLMOutcome()
    has_uncertainty = any(
        s in (CoverageStatus.NO_APPLICABLE_SKILL, CoverageStatus.UNVERIFIED)
        for s in diagnosis.node_type_coverage.values()
    )
    if llm_provider != "none" and not diagnosis.matches and has_uncertainty:
        _status(f"\n--- Falling back to LLM ({llm_provider}), grounded in real schema ---")
        llm = _invoke_llm_fallback(
            llm_provider, llm_model, llm_host, quick, query, plan, schemas
        )

    return AnalysisResult(diagnosis=diagnosis, plan=plan, schemas=schemas, llm=llm)


@app.command()
def analyze(
    dsn: str = typer.Option(..., help="PostgreSQL connection string"),
    query: str = typer.Option(..., help="The slow SQL query to diagnose"),
    llm_provider: str = typer.Option(
        "none",
        help="LLM fallback backend: none | ollama | claude | azure-openai | manual",
    ),
    llm_model: str = typer.Option(
        None,
        help="Model name for the chosen provider (e.g. 'gemma3:3b' for ollama). "
        "Uses each provider's built-in default if not set.",
    ),
    llm_host: str = typer.Option(
        None,
        help="Ollama host URL (e.g. 'http://localhost:11434'). Only used with "
        "--llm-provider ollama. Defaults to $OLLAMA_HOST or http://localhost:11434.",
    ),
    schema: str = typer.Option("public", help="Postgres schema name"),
    quick: bool = typer.Option(
        False,
        "--quick",
        help="With --llm-provider manual: print the prompt and exit "
        "immediately, without waiting for a pasted-back response or "
        "running validation. For fast ad-hoc checks where you'll just "
        "read the AI's answer yourself.",
    ),
):
    """Run EXPLAIN ANALYZE, match deterministic skills, optionally fall back to LLM."""

    def _echo(msg: str) -> None:
        typer.echo(msg)

    result = run_analysis(
        dsn=dsn,
        query=query,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_host=llm_host,
        schema=schema,
        quick=quick,
        on_status=_echo,
    )

    typer.echo("\n--- Execution plan summary ---")
    typer.echo(result.plan.summary())

    if result.matches:
        for m in result.matches:
            typer.secho(f"\n[{m.severity.upper()}] {m.skill_name}", fg=typer.colors.YELLOW, bold=True)
            typer.echo(m.explanation)
            typer.echo(f"Suggested fix:\n{m.fix_template}")
        return

    coverage = result.node_type_coverage
    if result.ledger_load_error:
        # Dev-mode: ledger missing → nudge to regenerate.
        # Frozen-binary: ledger corrupt/missing → this build is defective.
        severity = "MISSING" if result.ledger_status == LedgerStatus.MISSING else "CORRUPT"
        typer.secho(
            f"No skill flagged this query, but the coverage ledger failed to load "
            f"({severity}) — this result is UNVERIFIED, not confirmed clean. "
            f"Run the test suite to regenerate the ledger, or reinstall if using a packaged binary.",
            fg=typer.colors.RED,
        )
    elif all(s == CoverageStatus.SKILL_CLEARED for s in coverage.values()):
        typer.echo("No issues found — all node types examined and cleared by skill checks.")
    elif any(s in (CoverageStatus.NO_APPLICABLE_SKILL, CoverageStatus.UNVERIFIED) for s in coverage.values()):
        uncovered = [nt for nt, s in coverage.items() if s != CoverageStatus.SKILL_CLEARED]
        typer.echo(
            f"No deterministic skill matched. "
            f"Node type(s) not fully covered by skills: {', '.join(uncovered)}"
        )
    else:
        typer.echo("No deterministic skill matched.")

    if llm_provider == "none":
        typer.echo(
            "No LLM provider configured — re-run with --llm-provider "
            "ollama|claude|azure-openai for an AI-assisted hypothesis."
        )
        return

    # The LLM fallback already ran inside run_analysis() (it emitted the
    # "Falling back to LLM" status via on_status above). Print its outcome.
    llm = result.llm

    if llm.error:
        typer.secho(llm.error, fg=typer.colors.RED)
        raise typer.Exit(1)

    if llm.response is None or not llm.response.text:
        # manual --quick: the prompt was printed, nothing returned to validate.
        return

    typer.echo(
        f"\n--- LLM hypothesis ({llm.response.provider}/{llm.response.model}) ---"
    )
    typer.echo(llm.response.text.strip())

    if llm.validation is None:
        return
    if not llm.validation.ok:
        typer.secho(
            f"\n⚠ VALIDATION WARNING: mentions names not found in the real "
            f"schema: {', '.join(llm.validation.unknown_tokens)}. "
            "Treat this suggestion as unverified — do not apply blindly.",
            fg=typer.colors.RED,
            bold=True,
        )
    else:
        typer.secho(
            f"\n✓ All {llm.validation.checked_tokens} referenced identifiers matched "
            "the real schema.",
            fg=typer.colors.GREEN,
        )


@app.command()
def list_skills():
    """Print the loaded deterministic skill library."""
    loaded = load_skills()
    for s in loaded.skills:
        typer.echo(f"- {s.name} [{s.severity}]: {s.description.strip()}")


@app.command()
def ledger_status(
    ledger_path: str = typer.Option(
        None,
        help="Path to coverage ledger JSON. Defaults to the committed ledger.",
    ),
):
    """
    Report whether the coverage ledger loaded successfully.

    Output: LEDGER_STATUS=OK | MISSING | CORRUPT
    Exit code: 0=OK, 1=MISSING, 2=CORRUPT

    Designed for CI smoke checks — exit code alone is sufficient; no text
    parsing required.
    """
    path = Path(ledger_path) if ledger_path else DEFAULT_LEDGER_PATH
    loaded = load_skills(ledger_path=path)
    status = loaded.ledger_status
    typer.echo(f"LEDGER_STATUS={status.value.upper()}")
    if status == LedgerStatus.MISSING:
        raise typer.Exit(1)
    if status == LedgerStatus.CORRUPT:
        raise typer.Exit(2)


if __name__ == "__main__":
    app()
