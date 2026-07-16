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
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

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

def _dashboard_view(page: ft.Page, state: AppState) -> ft.View:
    status_color = ft.colors.GREEN if state.connection_ok else ft.colors.GREY_400
    status_label = "Connected" if state.connection_ok else "Not connected"
    status_icon = ft.icons.CIRCLE if state.connection_ok else ft.icons.CIRCLE_OUTLINED

    return ft.View(
        route="/",
        controls=[
            ft.AppBar(title=ft.Text("sql-doctor"), bgcolor=ft.colors.SURFACE_VARIANT),
            ft.Container(
                padding=40,
                content=ft.Column(
                    spacing=24,
                    controls=[
                        ft.Text("sql-doctor", size=32, weight=ft.FontWeight.BOLD),
                        ft.Text(
                            "Diagnose slow PostgreSQL queries with deterministic skill checks.",
                            size=16,
                            color=ft.colors.ON_SURFACE_VARIANT,
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
                                    icon=ft.icons.SEARCH,
                                    on_click=lambda _: page.go("/connect"),
                                ),
                                ft.OutlinedButton(
                                    "Connection Settings",
                                    icon=ft.icons.SETTINGS,
                                    on_click=lambda _: page.go("/connect"),
                                ),
                            ],
                        ),
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
    status_text = ft.Text("", color=ft.colors.ON_SURFACE_VARIANT)

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
        status_text.color = ft.colors.ON_SURFACE_VARIANT
        page.update()

        def _test():
            try:
                import psycopg2
                conn = psycopg2.connect(_dsn(), connect_timeout=5)
                conn.close()
                state.connection_ok = True
                status_text.value = "Connection successful."
                status_text.color = ft.colors.GREEN
            except Exception as exc:
                state.connection_ok = False
                status_text.value = f"Connection failed: {exc}"
                status_text.color = ft.colors.RED
            page.update()

        threading.Thread(target=_test, daemon=True).start()

    def on_continue(_) -> None:
        _save_fields()
        page.go("/analyze")

    return ft.View(
        route="/connect",
        controls=[
            ft.AppBar(
                title=ft.Text("Connection Settings"),
                bgcolor=ft.colors.SURFACE_VARIANT,
                leading=ft.IconButton(ft.icons.ARROW_BACK, on_click=lambda _: page.go("/")),
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
        font_family="monospace",
    )
    llm_dropdown = ft.Dropdown(
        label="LLM fallback",
        width=200,
        value="none",
        options=[
            ft.dropdown.Option("none", "None (deterministic only)"),
            ft.dropdown.Option("ollama", "Ollama (local)"),
            ft.dropdown.Option("claude", "Claude API"),
            ft.dropdown.Option("azure-openai", "Azure OpenAI"),
        ],
    )
    spinner = ft.ProgressRing(width=20, height=20, visible=False)
    status_text = ft.Text("", color=ft.colors.ON_SURFACE_VARIANT)
    analyze_btn = ft.ElevatedButton("Analyze", icon=ft.icons.PLAY_ARROW)

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

            statuses: list[str] = []

            def _on_status(msg: str) -> None:
                statuses.append(msg.strip())
                # Show last meaningful line only (skip blank/separator lines).
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
                result = run_analysis(
                    dsn=dsn,
                    query=state.last_query,
                    llm_provider=llm_dropdown.value or "none",
                    schema=cfg.schema,
                    on_status=_on_status,
                )
                state.last_result = result
                analyze_btn.disabled = False
                spinner.visible = False
                status_text.value = f"Done — {len(result.matches)} finding(s)."
                page.update()
                page.go("/results")
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
                bgcolor=ft.colors.SURFACE_VARIANT,
                leading=ft.IconButton(ft.icons.ARROW_BACK, on_click=lambda _: page.go("/")),
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

    tabs = ft.Tabs(
        selected_index=0,
        animation_duration=200,
        expand=True,
        tabs=[
            ft.Tab(text="Findings", content=ft.Container(padding=16, content=findings_content)),
            ft.Tab(text="Plan Tree", content=ft.Container(padding=16, content=plan_tree_content)),
        ],
    )

    export_status = ft.Text("", color=ft.colors.ON_SURFACE_VARIANT, size=12)

    def _on_save_result(e: ft.FilePickerResultEvent) -> None:
        if not e.path:
            return  # user cancelled
        try:
            cfg = state.config
            html = generate_html_report(
                result,
                query=state.last_query,
                host=cfg.host,
                dbname=cfg.dbname,
            )
            Path(e.path).write_text(html, encoding="utf-8")
            export_status.value = f"Saved: {e.path}"
            export_status.color = ft.colors.GREEN
        except Exception as exc:
            export_status.value = f"Export failed: {exc}"
            export_status.color = ft.colors.RED
        page.update()

    file_picker = ft.FilePicker(on_result=_on_save_result)
    page.overlay.append(file_picker)

    default_name = (
        "sql-doctor-report-"
        + datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        + ".html"
    )

    def on_export(_) -> None:
        file_picker.save_file(
            dialog_title="Save sql-doctor report",
            file_name=default_name,
            allowed_extensions=["html"],
        )

    return ft.View(
        route="/results",
        controls=[
            ft.AppBar(
                title=ft.Text("Results"),
                bgcolor=ft.colors.SURFACE_VARIANT,
                leading=ft.IconButton(
                    ft.icons.ARROW_BACK, on_click=lambda _: page.go("/analyze")
                ),
                actions=[
                    ft.TextButton(
                        "Export Report",
                        icon=ft.icons.DOWNLOAD,
                        on_click=on_export,
                    ),
                    ft.TextButton("New Analysis", on_click=lambda _: page.go("/analyze")),
                ],
            ),
            ft.Container(expand=True, content=tabs),
            ft.Container(
                padding=ft.padding.symmetric(horizontal=16, vertical=4),
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

    text_color = ft.colors.RED_400 if is_flagged else ft.colors.ON_SURFACE
    weight = ft.FontWeight.BOLD if is_flagged else ft.FontWeight.NORMAL
    prefix = "⚠ " if is_flagged else "· "

    node_row = ft.Container(
        padding=ft.padding.only(left=depth * 20, top=2, bottom=2),
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
        return ft.Text("No plan available.", color=ft.colors.ON_SURFACE_VARIANT)

    flagged_ids = {id(m.matched_node) for m in result.matches if m.matched_node is not None}

    legend_controls = []
    if flagged_ids:
        legend_controls.append(ft.Row(spacing=4, controls=[
            ft.Text("⚠", color=ft.colors.RED_400),
            ft.Text("Skill flagged this node", size=12),
        ]))
    legend_controls.append(ft.Row(spacing=4, controls=[
        ft.Text("·", color=ft.colors.ON_SURFACE),
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
# Findings tab (Steps 4-5 — detail panel wired here)
# ---------------------------------------------------------------------------

def _build_findings_tab(page: ft.Page, result) -> ft.Control:
    if not result.matches:
        return ft.Column(
            spacing=8,
            controls=[
                ft.Icon(ft.icons.CHECK_CIRCLE_OUTLINE, color=ft.colors.GREEN, size=48),
                ft.Text(
                    "No issues found — all node types examined and cleared.",
                    size=16,
                    color=ft.colors.ON_SURFACE_VARIANT,
                ),
            ],
        )

    _SEVERITY_COLOR = {
        "high": ft.colors.RED_400,
        "medium": ft.colors.ORANGE_400,
        "low": ft.colors.BLUE_400,
    }

    detail_panel = ft.Container(
        visible=False,
        padding=16,
        bgcolor=ft.colors.SURFACE_VARIANT,
        border_radius=8,
        content=ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO, controls=[]),
    )

    def _show_detail(match) -> None:
        detail_panel.content.controls = [
            ft.Row([
                ft.Container(
                    content=ft.Text(match.severity.upper(), size=11, weight=ft.FontWeight.BOLD, color=ft.colors.WHITE),
                    bgcolor=_SEVERITY_COLOR.get(match.severity, ft.colors.GREY),
                    padding=ft.padding.symmetric(horizontal=8, vertical=2),
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
        sev_color = _SEVERITY_COLOR.get(match.severity, ft.colors.GREY)
        row = ft.ListTile(
            leading=ft.Container(
                content=ft.Text(
                    match.severity.upper(),
                    size=10,
                    weight=ft.FontWeight.BOLD,
                    color=ft.colors.WHITE,
                ),
                bgcolor=sev_color,
                padding=ft.padding.symmetric(horizontal=6, vertical=2),
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
                color=ft.colors.ON_SURFACE_VARIANT,
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
# App entry point
# ---------------------------------------------------------------------------

def main(page: ft.Page) -> None:
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

    def route_change(e: ft.RouteChangeEvent) -> None:
        route = e.route
        builder = _VIEW_BUILDERS.get(route, _dashboard_view)
        page.views.clear()
        page.views.append(builder(page, state))
        page.update()

    def view_pop(_) -> None:
        page.views.pop()
        top = page.views[-1] if page.views else None
        if top:
            page.go(top.route)

    page.on_route_change = route_change
    page.on_view_pop = view_pop
    page.go("/")


if __name__ == "__main__":
    ft.app(target=main)
