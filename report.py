"""
HTML report generation for sql-doctor.

generate_html_report() produces a fully self-contained HTML string (inline CSS,
no external dependencies) suitable for writing to a .html file and opening in
any browser.

The report contains:
  - Header: query text, timestamp, host/database (no password)
  - Summary line: finding counts by severity
  - Findings table: one row per SkillMatch with full explanation + fix_template
    inline (no click-to-expand needed in a static document)
  - Plan Tree: nested HTML mirroring the GUI's color-coded tree

Kept intentionally separate from gui.py so it can be unit-tested and
called from a CLI export command in future without importing Flet.
"""

from __future__ import annotations

import html
from datetime import datetime


# ---------------------------------------------------------------------------
# Shared pure helper — used by both the Flet tree and the HTML tree
# ---------------------------------------------------------------------------

def node_label(node) -> str:
    """
    Build the human-readable label for a single PlanNode.
    Pure Python, no Flet or HTML — called by both renderers.
    """
    parts = [node.node_type]
    if node.relation_name:
        parts.append(f"on {node.relation_name}")
    if node.index_name:
        parts.append(f"via {node.index_name}")
    parts.append(f"({int(node.actual_rows)} rows, {node.actual_total_time:.1f} ms)")
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# Internal HTML helpers
# ---------------------------------------------------------------------------

_SEVERITY_BG = {
    "high":   "#e53935",
    "medium": "#fb8c00",
    "low":    "#1e88e5",
}
_SEVERITY_LABEL_CSS = (
    "display:inline-block;padding:2px 8px;border-radius:4px;"
    "font-size:11px;font-weight:bold;color:#fff;"
)


def _h(text: str) -> str:
    """HTML-escape a plain string."""
    return html.escape(str(text))


def _severity_badge(severity: str) -> str:
    bg = _SEVERITY_BG.get(severity, "#757575")
    return (
        f'<span style="{_SEVERITY_LABEL_CSS}background:{bg}">'
        f'{_h(severity.upper())}</span>'
    )


def _render_html_node(node, depth: int, flagged_ids: set) -> str:
    """
    Recursively render one PlanNode and its children as nested HTML divs.

    Red bold:   node was flagged by at least one skill.
    Default:    clean node.

    flagged_ids is a set of id() values of matched PlanNode objects.
    PlanNode is an unhashable mutable dataclass (eq=True, frozen=False), so
    we track identity via id() rather than placing nodes directly in a set.
    The id() values remain stable for the lifetime of the AnalysisResult.
    """
    is_flagged = id(node) in flagged_ids
    label = node_label(node)
    indent = depth * 20

    if is_flagged:
        prefix = "⚠ "
        style = f"padding-left:{indent}px;color:#e53935;font-weight:bold;"
    else:
        prefix = "· "
        style = f"padding-left:{indent}px;color:#212121;"

    row = f'<div style="font-family:monospace;font-size:13px;{style}">{prefix}{_h(label)}</div>\n'
    children = "".join(_render_html_node(child, depth + 1, flagged_ids) for child in node.children)
    return row + children


def _render_findings_html(matches: list) -> str:
    if not matches:
        return (
            '<p style="color:#388e3c;font-size:16px">'
            '✓ No issues found — all node types examined and cleared.</p>'
        )

    rows = []
    for m in matches:
        explanation = _h(m.explanation.strip()).replace("\n", "<br>")
        fix = _h(m.fix_template.strip()).replace("\n", "<br>")
        rows.append(f"""
  <div style="border:1px solid #e0e0e0;border-radius:6px;padding:16px;margin-bottom:16px;">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;">
      {_severity_badge(m.severity)}
      <span style="font-size:16px;font-weight:600">{_h(m.skill_name)}</span>
    </div>
    <p style="margin:0 0 4px;color:#616161;font-size:13px">{_h(m.description.strip())}</p>
    <hr style="border:none;border-top:1px solid #eeeeee;margin:10px 0">
    <p style="margin:0 0 4px;font-weight:600;font-size:13px">Explanation</p>
    <p style="margin:0 0 12px;white-space:pre-wrap">{explanation}</p>
    <p style="margin:0 0 4px;font-weight:600;font-size:13px">Suggested fix</p>
    <pre style="margin:0;background:#f5f5f5;padding:10px;border-radius:4px;white-space:pre-wrap;font-size:13px">{fix}</pre>
  </div>""")

    return "\n".join(rows)


def _render_plan_tree_html(result) -> str:
    if result is None or result.plan is None:
        return "<p>No plan available.</p>"

    flagged_ids = {id(m.matched_node) for m in result.matches if m.matched_node is not None}
    tree_html = _render_html_node(result.plan.root, depth=0, flagged_ids=flagged_ids)

    legend_parts = []
    if flagged_ids:
        legend_parts.append('<span style="color:#e53935;font-weight:bold">⚠</span> Skill flagged this node')
    legend_parts.append('<span style="color:#212121">·</span> Clean')
    legend = (
        '<p style="font-size:12px;color:#616161;margin-bottom:8px">'
        + '&emsp;'.join(legend_parts)
        + '</p>'
    )
    return legend + f'<div style="line-height:1.6">{tree_html}</div>'


def _count_by_severity(matches: list) -> str:
    if not matches:
        return "No findings."
    counts: dict[str, int] = {}
    for m in matches:
        counts[m.severity] = counts.get(m.severity, 0) + 1
    parts = [f"{counts.get(s, 0)} {s}" for s in ("high", "medium", "low") if counts.get(s)]
    total = len(matches)
    return f"{total} finding{'s' if total != 1 else ''} — {', '.join(parts)}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_html_report(result, query: str, host: str, dbname: str) -> str:
    """
    Generate a fully self-contained HTML report string.

    Parameters
    ----------
    result  : AnalysisResult from cli.run_analysis()
    query   : the SQL query that was analyzed (displayed verbatim)
    host    : database host (for the header — no password)
    dbname  : database name (for the header)

    Returns a complete HTML document as a string, ready to write to a .html file.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary = _count_by_severity(result.matches if result else [])
    findings_html = _render_findings_html(result.matches if result else [])
    plan_html = _render_plan_tree_html(result)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>sql-doctor report — {_h(dbname)} — {_h(timestamp)}</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      max-width: 900px;
      margin: 40px auto;
      padding: 0 24px;
      color: #212121;
      background: #fff;
    }}
    h1 {{ font-size: 24px; margin-bottom: 4px; }}
    h2 {{ font-size: 18px; margin: 32px 0 12px; border-bottom: 2px solid #eeeeee; padding-bottom: 4px; }}
    .meta {{ color: #616161; font-size: 13px; margin-bottom: 8px; }}
    .summary {{
      display: inline-block;
      background: #f5f5f5;
      border-radius: 6px;
      padding: 8px 16px;
      font-size: 14px;
      font-weight: 600;
      margin-bottom: 24px;
    }}
    pre.query {{
      background: #f5f5f5;
      padding: 12px;
      border-radius: 6px;
      font-size: 13px;
      white-space: pre-wrap;
      word-break: break-all;
    }}
  </style>
</head>
<body>

<h1>sql-doctor report</h1>
<p class="meta">Generated: {_h(timestamp)}</p>
<p class="meta">Host: {_h(host)}&ensp;|&ensp;Database: {_h(dbname)}</p>

<h2>Query</h2>
<pre class="query">{_h(query.strip())}</pre>

<div class="summary">{_h(summary)}</div>

<h2>Findings</h2>
{findings_html}

<h2>Plan Tree</h2>
{plan_html}

</body>
</html>"""
