"""
sql-doctor GUI — Flet desktop app.

Entry point: python gui.py   (or the built sql-doctor-gui.exe)

Screen flow:
    / (Dashboard)  →  /connect (Connection setup)
                   →  /analyze (SQL input + progress)
                   →  /results (Findings + Plan Tree tabs)

Config file: %APPDATA%\\sql-doctor\\config.json
  Stores: host, port, user, dbname, schema
  Does NOT store the password — prompted each session.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from core.skill_matcher import default_external_skills_dir, ensure_external_skills_dir

import flet as ft

# ---------------------------------------------------------------------------
# Config persistence (host/user/db only — no credentials)
# ---------------------------------------------------------------------------

_CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "sql-doctor"
_CONFIG_FILE = _CONFIG_DIR / "config.json"


@dataclass
class ConnectionConfig:
    host: str = "localhost"
    port: str = "5432"
    user: str = ""
    dbname: str = ""
    schema: str = "public"


def _load_config() -> ConnectionConfig:
    try:
        data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        return ConnectionConfig(**{k: v for k, v in data.items() if k in ConnectionConfig.__dataclass_fields__})
    except Exception:
        return ConnectionConfig()


def _save_config(cfg: ConnectionConfig) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _CONFIG_FILE.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Shared app state (passed between screens via closure)
# ---------------------------------------------------------------------------

@dataclass
class AppState:
    config: ConnectionConfig = field(default_factory=_load_config)
    password: str = ""                  # never persisted
    last_query: str = ""
    last_result: object = None          # AnalysisResult from run_analysis()
    connection_ok: bool = False


# ---------------------------------------------------------------------------
# Screen builders
# ---------------------------------------------------------------------------

def _open_in_file_manager(path: Path) -> None:
    """Open a folder in the OS file manager (best effort, cross-platform)."""
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 — opening a folder we created
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _dashboard_view(page: ft.Page, state: AppState) -> ft.View:
    status_color = ft.Colors.GREEN if state.connection_ok else ft.Colors.GREY_400
    status_label = "Connected" if state.connection_ok else "Not connected"
    status_icon = ft.Icons.CIRCLE if state.connection_ok else ft.Icons.RADIO_BUTTON_UNCHECKED

    skills_hint = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)

    def on_open_skills_folder(_) -> None:
        # Create the folder (+ starter README) on first use, then reveal it so a
        # non-technical colleague can drop in extra skill YAMLs without knowing
        # the path. New skills are picked up on the next analysis.
        try:
            folder = ensure_external_skills_dir()
            _open_in_file_manager(folder)
            skills_hint.value = f"Add *.yaml skills to: {folder}"
            skills_hint.color = ft.Colors.ON_SURFACE_VARIANT
        except Exception as exc:  # noqa: BLE001
            skills_hint.value = f"Could not open skills folder: {exc}"
            skills_hint.color = ft.Colors.RED
        page.update()

    return ft.View(
        route="/",
        controls=[
            ft.AppBar(title=ft.Text("sql-doctor"), bgcolor=ft.Colors.SURFACE_CONTAINER),
            ft.Container(
                padding=40,
                content=ft.Column(
                    spacing=24,
                    controls=[
                        ft.Text("sql-doctor", size=32, weight=ft.FontWeight.BOLD),
                        ft.Text(
                            "Diagnose slow PostgreSQL queries with deterministic skill checks.",
                            size=16,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                        ft.Row(
                            spacing=8,
                            controls=[
                                ft.Icon(status_icon, color=status_color, size=16),
                                ft.Text(status_label, color=status_color),
                            ],
                        ),
                        ft.Row(
                            spacing=12,
                            controls=[
                                ft.ElevatedButton(
                                    "New Analysis",
                                    icon=ft.Icons.SEARCH,
                                    on_click=lambda _: page.navigate("/connect"),
                                ),
                                ft.OutlinedButton(
                                    "Connection Settings",
                                    icon=ft.Icons.SETTINGS,
                                    on_click=lambda _: page.navigate("/connect"),
                                ),
                                ft.OutlinedButton(
                                    "Open skills folder",
                                    icon=ft.Icons.FOLDER_OPEN,
                                    on_click=on_open_skills_folder,
                                ),
                            ],
                        ),
                        skills_hint,
                    ],
                ),
            ),
        ],
    )


def _connect_view(page: ft.Page, state: AppState) -> ft.View:
    cfg = state.config

    host_field = ft.TextField(label="Host", value=cfg.host, expand=True)
    port_field = ft.TextField(label="Port", value=cfg.port, width=100)
    user_field = ft.TextField(label="Username", value=cfg.user, expand=True)
    password_field = ft.TextField(
        label="Password (this session only — not saved)",
        password=True,
        can_reveal_password=True,
        expand=True,
    )
    dbname_field = ft.TextField(label="Database", value=cfg.dbname, expand=True)
    schema_field = ft.TextField(label="Schema", value=cfg.schema, width=160)
    status_text = ft.Text("", color=ft.Colors.ON_SURFACE_VARIANT)

    def _dsn() -> str:
        return (
            f"postgresql://{user_field.value}:{password_field.value}"
            f"@{host_field.value}:{port_field.value}/{dbname_field.value}"
        )

    def _save_fields() -> None:
        state.config = ConnectionConfig(
            host=host_field.value,
            port=port_field.value,
            user=user_field.value,
            dbname=dbname_field.value,
            schema=schema_field.value,
        )
        state.password = password_field.value
        _save_config(state.config)

    def on_test(_) -> None:
        status_text.value = "Testing connection..."
        status_text.color = ft.Colors.ON_SURFACE_VARIANT
        page.update()

        def _test():
            try:
                import psycopg2
                conn = psycopg2.connect(_dsn(), connect_timeout=5)
                conn.close()
                state.connection_ok = True
                status_text.value = "Connection successful."
                status_text.color = ft.Colors.GREEN
            except Exception as exc:
                state.connection_ok = False
                status_text.value = f"Connection failed: {exc}"
                status_text.color = ft.Colors.RED
            page.update()

        threading.Thread(target=_test, daemon=True).start()

    def on_continue(_) -> None:
        _save_fields()
        page.navigate("/analyze")

    return ft.View(
        route="/connect",
        controls=[
            ft.AppBar(
                title=ft.Text("Connection Settings"),
                bgcolor=ft.Colors.SURFACE_CONTAINER,
                leading=ft.IconButton(ft.Icons.ARROW_BACK, on_click=lambda _: page.navigate("/")),
            ),
            ft.Container(
                padding=40,
                content=ft.Column(
                    spacing=16,
                    controls=[
                        ft.Row([host_field, port_field], spacing=12),
                        ft.Row([user_field], spacing=12),
                        ft.Row([password_field], spacing=12),
                        ft.Row([dbname_field, schema_field], spacing=12),
                        ft.Row(
                            spacing=12,
                            controls=[
                                ft.OutlinedButton("Test Connection", on_click=on_test),
                                ft.ElevatedButton("Continue →", on_click=on_continue),
                            ],
                        ),
                        status_text,
                    ],
                ),
            ),
        ],
    )


def _analyze_view(page: ft.Page, state: AppState) -> ft.View:
    query_field = ft.TextField(
        label="SQL query to diagnose",
        multiline=True,
        min_lines=6,
        max_lines=16,
        value=state.last_query,
        expand=True,
        hint_text="SELECT * FROM transactions WHERE account_id = 42",
        text_style=ft.TextStyle(font_family="monospace"),
    )
    # Model name + host for Ollama. Only shown when "Ollama (local)" is selected;
    # OllamaProvider defaults to "sqlcoder" (rarely pulled) and localhost:11434.
    model_field = ft.TextField(
        label="Ollama model",
        value="qwen2.5-coder:7b",
        width=220,
        visible=False,
    )
    host_field = ft.TextField(
        label="Ollama host",
        value="http://localhost:11434",
        width=240,
        visible=False,
    )

    def on_provider_change(_) -> None:
        is_ollama = llm_dropdown.value == "ollama"
        model_field.visible = is_ollama
        host_field.visible = is_ollama
        page.update()

    llm_dropdown = ft.Dropdown(
        label="LLM fallback",
        width=200,
        value="none",
        on_select=on_provider_change,
        options=[
            ft.dropdown.Option("none", "None (deterministic only)"),
            ft.dropdown.Option("ollama", "Ollama (local)"),
            ft.dropdown.Option("claude", "Claude API"),
            ft.dropdown.Option("azure-openai", "Azure OpenAI"),
        ],
    )
    spinner = ft.ProgressRing(width=20, height=20, visible=False)
    status_text = ft.Text("", color=ft.Colors.ON_SURFACE_VARIANT)
    analyze_btn = ft.ElevatedButton("Analyze", icon=ft.Icons.PLAY_ARROW)

    def on_analyze(_) -> None:
        state.last_query = query_field.value
        if not query_field.value.strip():
            status_text.value = "Enter a SQL query first."
            page.update()
            return

        analyze_btn.disabled = True
        spinner.visible = True
        status_text.value = ""
        page.update()

        def _run():
            from cli import run_analysis

            def _on_status(msg: str) -> None:
                display = msg.strip().lstrip("-").strip()
                if display:
                    status_text.value = display
                    page.update()

            try:
                cfg = state.config
                dsn = (
                    f"postgresql://{cfg.user}:{state.password}"
                    f"@{cfg.host}:{cfg.port}/{cfg.dbname}"
                )
                # Only pass model/host for Ollama; other providers have their own
                # defaults and these fields are hidden/irrelevant for them.
                is_ollama = llm_dropdown.value == "ollama"
                llm_model = (
                    model_field.value.strip()
                    if is_ollama and model_field.value.strip()
                    else None
                )
                llm_host = (
                    host_field.value.strip()
                    if is_ollama and host_field.value.strip()
                    else None
                )
                result = run_analysis(
                    dsn=dsn,
                    query=state.last_query,
                    llm_provider=llm_dropdown.value or "none",
                    llm_model=llm_model,
                    llm_host=llm_host,
                    schema=cfg.schema,
                    on_status=_on_status,
                    # Merge any user-added skills from the external folder.
                    external_skills_dir=default_external_skills_dir(),
                )
                state.last_result = result
                analyze_btn.disabled = False
                spinner.visible = False
                status_text.value = f"Done — {len(result.matches)} finding(s)."
                page.update()
                # Navigation must run on the page's event loop. This code is on a
                # background thread (no running loop), so navigate()/create_task()
                # would raise "no running event loop". run_task() marshals the
                # push_route coroutine onto the loop thread-safely.
                page.run_task(page.push_route, "/results")
            except Exception as exc:
                analyze_btn.disabled = False
                spinner.visible = False
                status_text.value = f"Error: {exc}"
                page.update()

        threading.Thread(target=_run, daemon=True).start()

    analyze_btn.on_click = on_analyze

    return ft.View(
        route="/analyze",
        controls=[
            ft.AppBar(
                title=ft.Text("Analyze Query"),
                bgcolor=ft.Colors.SURFACE_CONTAINER,
                leading=ft.IconButton(ft.Icons.ARROW_BACK, on_click=lambda _: page.navigate("/")),
            ),
            ft.Container(
                padding=40,
                expand=True,
                content=ft.Column(
                    expand=True,
                    spacing=16,
                    controls=[
                        ft.Row([query_field], expand=True),
                        ft.Row(
                            spacing=12,
                            controls=[
                                llm_dropdown,
                                model_field,
                                host_field,
                                analyze_btn,
                                spinner,
                                status_text,
                            ],
                        ),
                    ],
                ),
            ),
        ],
    )


def _results_view(page: ft.Page, state: AppState) -> ft.View:
    from datetime import datetime
    from report import generate_html_report

    result = state.last_result

    # --- Findings tab ---
    findings_content = _build_findings_tab(page, result) if result else ft.Text("No results yet.")

    # --- Plan Tree tab ---
    plan_tree_content = _build_plan_tree_tab(result) if result else ft.Text("No results yet.")

    # --- LLM Hypothesis tab ---
    llm_content = _build_llm_tab(result) if result else ft.Text("No results yet.")

    # Flet 0.86 Tabs model: Tabs(length=, content=Column([TabBar, TabBarView])).
    # Tab is header-only (label/icon); tab bodies live in a parallel TabBarView
    # whose controls are matched to the TabBar tabs by index order.
    tabs = ft.Tabs(
        length=3,
        expand=True,
        content=ft.Column(
            expand=True,
            controls=[
                ft.TabBar(
                    tabs=[
                        ft.Tab(label="Findings"),
                        ft.Tab(label="Plan Tree"),
                        ft.Tab(label="LLM Hypothesis", icon=ft.Icons.AUTO_AWESOME),
                    ],
                ),
                ft.TabBarView(
                    expand=True,
                    controls=[
                        ft.Container(padding=16, content=findings_content),
                        ft.Container(padding=16, content=plan_tree_content),
                        ft.Container(padding=16, content=llm_content),
                    ],
                ),
            ],
        ),
    )

    export_status = ft.Text("", color=ft.Colors.ON_SURFACE_VARIANT, size=12)

    # FilePicker is a Service in Flet 0.86 (not a visual Control), so it must be
    # registered in page.services — not page.overlay, which renders its members
    # as controls and raises "Unknown control: FilePicker".
    file_picker = ft.FilePicker()
    page.services.append(file_picker)

    default_name = (
        "sql-doctor-report-"
        + datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        + ".html"
    )

    async def on_export(_) -> None:
        path = await file_picker.save_file(
            dialog_title="Save sql-doctor report",
            file_name=default_name,
            allowed_extensions=["html"],
        )
        if path is None:
            return  # user cancelled
        try:
            cfg = state.config
            html_content = generate_html_report(
                result,
                query=state.last_query,
                host=cfg.host,
                dbname=cfg.dbname,
            )
            Path(path).write_text(html_content, encoding="utf-8")
            export_status.value = f"Saved: {path}"
            export_status.color = ft.Colors.GREEN
        except Exception as exc:
            export_status.value = f"Export failed: {exc}"
            export_status.color = ft.Colors.RED
        page.update()

    return ft.View(
        route="/results",
        controls=[
            ft.AppBar(
                title=ft.Text("Results"),
                bgcolor=ft.Colors.SURFACE_CONTAINER,
                leading=ft.IconButton(
                    ft.Icons.ARROW_BACK, on_click=lambda _: page.navigate("/analyze")
                ),
                actions=[
                    ft.TextButton(
                        "Export Report",
                        icon=ft.Icons.DOWNLOAD,
                        on_click=on_export,
                    ),
                    ft.TextButton("New Analysis", on_click=lambda _: page.navigate("/analyze")),
                ],
            ),
            ft.Container(expand=True, content=tabs),
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=16, vertical=4),
                content=export_status,
            ),
        ],
        expand=True,
    )


# ---------------------------------------------------------------------------
# Plan Tree tab — recursive, color-coded by skill match
# ---------------------------------------------------------------------------

def _render_plan_node(node, depth: int, flagged_ids: set) -> ft.Control:
    """
    Recursively render one PlanNode and its children as indented Flet controls.

    Red:   this node was flagged by at least one skill.
    White: clean node.

    flagged_ids is a set of id() values of matched PlanNode objects.
    PlanNode is an unhashable mutable dataclass (eq=True, frozen=False), so
    we track identity via id() rather than placing nodes directly in a set.
    """
    is_flagged = id(node) in flagged_ids

    label_parts = [node.node_type]
    if node.relation_name:
        label_parts.append(f"on {node.relation_name}")
    if node.index_name:
        label_parts.append(f"via {node.index_name}")
    label_parts.append(
        f"({int(node.actual_rows)} rows, {node.actual_total_time:.1f} ms)"
    )
    label = "  ".join(label_parts)

    text_color = ft.Colors.RED_400 if is_flagged else ft.Colors.ON_SURFACE
    weight = ft.FontWeight.BOLD if is_flagged else ft.FontWeight.NORMAL
    prefix = "⚠ " if is_flagged else "· "

    node_row = ft.Container(
        padding=ft.Padding.only(left=depth * 20, top=2, bottom=2),
        content=ft.Text(
            prefix + label,
            color=text_color,
            weight=weight,
            size=13,
            font_family="monospace",
            selectable=True,
        ),
    )

    child_controls = [_render_plan_node(child, depth + 1, flagged_ids) for child in node.children]
    return ft.Column(controls=[node_row, *child_controls], spacing=0)


def _build_plan_tree_tab(result) -> ft.Control:
    if result is None or result.plan is None:
        return ft.Text("No plan available.", color=ft.Colors.ON_SURFACE_VARIANT)

    flagged_ids = {id(m.matched_node) for m in result.matches if m.matched_node is not None}

    legend_controls = []
    if flagged_ids:
        legend_controls.append(ft.Row(spacing=4, controls=[
            ft.Text("⚠", color=ft.Colors.RED_400),
            ft.Text("Skill flagged this node", size=12),
        ]))
    legend_controls.append(ft.Row(spacing=4, controls=[
        ft.Text("·", color=ft.Colors.ON_SURFACE),
        ft.Text("Clean", size=12),
    ]))
    legend = ft.Row(spacing=16, controls=legend_controls)

    tree = _render_plan_node(result.plan.root, depth=0, flagged_ids=flagged_ids)

    return ft.Column(
        spacing=8,
        expand=True,
        scroll=ft.ScrollMode.AUTO,
        controls=[legend, ft.Divider(), tree],
    )


# ---------------------------------------------------------------------------
# Findings tab (detail panel wired here)
# ---------------------------------------------------------------------------

def _build_findings_tab(page: ft.Page, result) -> ft.Control:
    if not result.matches:
        return ft.Column(
            spacing=8,
            controls=[
                ft.Icon(ft.Icons.CHECK_CIRCLE_OUTLINE, color=ft.Colors.GREEN, size=48),
                ft.Text(
                    "No issues found — all node types examined and cleared.",
                    size=16,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                ),
            ],
        )

    _SEVERITY_COLOR = {
        "high": ft.Colors.RED_400,
        "medium": ft.Colors.ORANGE_400,
        "low": ft.Colors.BLUE_400,
    }

    detail_panel = ft.Container(
        visible=False,
        padding=16,
        bgcolor=ft.Colors.SURFACE_CONTAINER,
        border_radius=8,
        content=ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO, controls=[]),
    )

    def _show_detail(match) -> None:
        detail_panel.content.controls = [
            ft.Row([
                ft.Container(
                    content=ft.Text(match.severity.upper(), size=11, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
                    bgcolor=_SEVERITY_COLOR.get(match.severity, ft.Colors.GREY),
                    padding=ft.Padding.symmetric(horizontal=8, vertical=2),
                    border_radius=4,
                ),
                ft.Text(match.skill_name, size=16, weight=ft.FontWeight.BOLD),
            ]),
            ft.Divider(),
            ft.Text("Explanation", weight=ft.FontWeight.BOLD, size=13),
            ft.Text(match.explanation.strip(), selectable=True),
            ft.Text("Suggested fix", weight=ft.FontWeight.BOLD, size=13),
            ft.Text(match.fix_template.strip(), selectable=True, font_family="monospace"),
            ft.TextButton("Close", on_click=lambda _: _hide_detail()),
        ]
        detail_panel.visible = True
        page.update()

    def _hide_detail() -> None:
        detail_panel.visible = False
        page.update()

    rows = []
    for match in result.matches:
        sev_color = _SEVERITY_COLOR.get(match.severity, ft.Colors.GREY)
        row = ft.ListTile(
            leading=ft.Container(
                content=ft.Text(
                    match.severity.upper(),
                    size=10,
                    weight=ft.FontWeight.BOLD,
                    color=ft.Colors.WHITE,
                ),
                bgcolor=sev_color,
                padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                border_radius=4,
            ),
            title=ft.Text(match.skill_name, weight=ft.FontWeight.W_500),
            subtitle=ft.Text(match.description.strip(), max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            on_click=lambda _, m=match: _show_detail(m),
        )
        rows.append(row)

    return ft.Column(
        spacing=0,
        controls=[
            ft.Text(
                f"{len(result.matches)} finding(s) — click a row for details",
                size=12,
                color=ft.Colors.ON_SURFACE_VARIANT,
            ),
            ft.Divider(),
            *rows,
            ft.Divider(),
            detail_panel,
        ],
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )


# ---------------------------------------------------------------------------
# LLM Hypothesis tab — shows the grounded LLM fallback outcome, if any
# ---------------------------------------------------------------------------

def _build_llm_tab(result) -> ft.Control:
    llm = getattr(result, "llm", None)

    # The fallback only runs when a provider is selected AND no skill matched AND
    # the plan has genuine uncertainty. When it did NOT run, say exactly why —
    # never a silent blank.
    if llm is None or not getattr(llm, "attempted", False):
        reason = getattr(llm, "skipped_reason", None) if llm is not None else None
        n = len(result.matches) if result is not None else 0
        if reason == "deterministic_findings":
            detail = (
                f"No AI call was made — {n} deterministic finding"
                f"{'s' if n != 1 else ''} already explain this plan. "
                "The LLM fallback runs only when the deterministic skills don't "
                "cover the query. See the Findings tab."
            )
        elif reason == "fully_cleared":
            detail = (
                "No AI call was made — every node was examined and cleared by "
                "ledger-backed skills (proven clean), so there was nothing for the "
                "AI to second-guess."
            )
        elif reason == "no_provider":
            detail = (
                "No AI call was made — the LLM fallback was set to None. Re-run "
                "with an Ollama / Claude / Azure provider to get an AI hypothesis "
                "when the deterministic skills don't cover the plan."
            )
        else:
            detail = (
                "No AI call was made for this query."
            )
        return ft.Column(
            spacing=8,
            controls=[
                ft.Row(spacing=8, controls=[
                    ft.Icon(ft.Icons.INFO_OUTLINE, color=ft.Colors.ON_SURFACE_VARIANT),
                    ft.Text("No LLM hypothesis", size=16, weight=ft.FontWeight.BOLD),
                ]),
                ft.Text(detail, color=ft.Colors.ON_SURFACE_VARIANT),
            ],
        )

    if llm.error:
        return ft.Column(
            spacing=8,
            controls=[
                ft.Row(spacing=8, controls=[
                    ft.Icon(ft.Icons.ERROR_OUTLINE, color=ft.Colors.RED_400),
                    ft.Text("LLM fallback failed", weight=ft.FontWeight.BOLD, size=16),
                ]),
                ft.Text(llm.error, color=ft.Colors.RED_400, selectable=True),
            ],
        )

    resp = llm.response
    if resp is None or not resp.text.strip():
        return ft.Text(
            "The provider returned no hypothesis text.",
            color=ft.Colors.ON_SURFACE_VARIANT,
        )

    controls = [
        ft.Row(spacing=8, controls=[
            ft.Icon(ft.Icons.AUTO_AWESOME, color=ft.Colors.BLUE_400),
            ft.Text(
                f"Hypothesis ({resp.provider}/{resp.model})",
                weight=ft.FontWeight.BOLD,
                size=16,
            ),
        ]),
        ft.Text(
            "Grounded in the real schema, but treat as a hypothesis to verify — "
            "not a confirmed diagnosis.",
            size=12,
            color=ft.Colors.ON_SURFACE_VARIANT,
        ),
        ft.Divider(),
        ft.Text(resp.text.strip(), selectable=True),
        ft.Divider(),
    ]

    # Post-LLM identifier grounding check.
    v = llm.validation
    if v is not None and not v.ok:
        controls.append(ft.Row(spacing=8, controls=[
            ft.Icon(ft.Icons.WARNING_AMBER, color=ft.Colors.ORANGE_400),
            ft.Text(
                "Validation warning: mentions names not found in the real schema: "
                + ", ".join(v.unknown_tokens)
                + ". Treat this suggestion as unverified — do not apply blindly.",
                color=ft.Colors.ORANGE_400,
                selectable=True,
                expand=True,
            ),
        ]))
    elif v is not None:
        controls.append(ft.Row(spacing=8, controls=[
            ft.Icon(ft.Icons.CHECK_CIRCLE_OUTLINE, color=ft.Colors.GREEN),
            ft.Text(
                f"All {v.checked_tokens} referenced identifiers matched the real schema.",
                color=ft.Colors.GREEN,
            ),
        ]))

    return ft.Column(spacing=8, controls=controls, scroll=ft.ScrollMode.AUTO, expand=True)


# ---------------------------------------------------------------------------
# App entry point
# ---------------------------------------------------------------------------

async def main(page: ft.Page) -> None:
    page.title = "sql-doctor"
    page.theme_mode = ft.ThemeMode.SYSTEM
    page.window.width = 900
    page.window.height = 700

    state = AppState()

    _VIEW_BUILDERS = {
        "/": _dashboard_view,
        "/connect": _connect_view,
        "/analyze": _analyze_view,
        "/results": _results_view,
    }

    def render_route(route: str) -> None:
        builder = _VIEW_BUILDERS.get(route, _dashboard_view)
        try:
            view = builder(page, state)
        except Exception:
            traceback.print_exc()
            view = ft.View(
                route=route,
                controls=[ft.Text(f"Error loading {route} — see console", color=ft.Colors.RED)],
            )
        page.views.clear()
        page.views.append(view)
        page.update()

    def route_change(e: ft.RouteChangeEvent) -> None:
        render_route(e.route)

    async def view_pop(e) -> None:
        if e.view is not None:
            page.views.remove(e.view)
        top = page.views[-1] if page.views else None
        if top:
            await page.push_route(top.route)

    page.on_route_change = route_change
    page.on_view_pop = view_pop
    # Render the initial route directly: page.route already defaults to "/",
    # so push_route("/") is a no-op that never fires on_route_change. Later
    # navigations push a *different* route and fire the handler normally.
    render_route(page.route or "/")


def _self_check() -> int:
    """
    Verify the bundled skill library and coverage ledger resolve at runtime.

    build.py runs this as a post-build integrity check on the frozen binary.
    It exercises the exact importlib.resources path resolution that
    run_analysis() relies on (DEFAULT_LEDGER_PATH + DEFAULT_SKILLS_DIR), without
    opening a window or needing a database, and signals the result via exit
    code — because a --noconsole packed exe may have no usable stdout.

    Exit 0 = both the skills/ library and the coverage ledger were found inside
    the bundle and the ledger loaded cleanly; non-zero = defective build.
    """
    try:
        from core.skill_matcher import DEFAULT_LEDGER_PATH, LedgerStatus, load_skills

        loaded = load_skills(ledger_path=DEFAULT_LEDGER_PATH)
    except Exception as exc:  # noqa: BLE001
        print(f"SELF-CHECK FAILED: could not load bundled skills/ledger: {exc}")
        return 1

    if not loaded.skills:
        print("SELF-CHECK FAILED: no skills found in the bundle (skills/ not packaged?)")
        return 1
    if loaded.ledger_status != LedgerStatus.OK:
        print(
            f"SELF-CHECK FAILED: coverage ledger status {loaded.ledger_status.name} "
            "(expected OK) — the bundled ledger is missing or corrupt"
        )
        return 1

    print(f"SELF-CHECK OK: {len(loaded.skills)} skills loaded, coverage ledger OK")
    return 0


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        # Post-build integrity check (see build.py Step 6). Must run BEFORE
        # ft.run(), which would otherwise open a blocking window.
        raise SystemExit(_self_check())
    ft.run(main)
