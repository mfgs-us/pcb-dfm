from __future__ import annotations

import html as _html
from typing import Optional

from .remediation import remediation_for
from .results import DfmResult


def summarize_status(counts) -> str:
    return (
        f"info: {counts.info}, "
        f"warning: {counts.warning}, "
        f"error: {counts.error}, "
        f"critical: {counts.critical}"
    )


def generate_text_report(result: DfmResult) -> str:
    lines = []

    lines.append(f"DFM Report for {result.design.name}")
    if result.design.revision:
        lines.append(f"Revision: {result.design.revision}")
    lines.append(f"Ruleset:  {result.ruleset.name} {result.ruleset.version}")
    lines.append("")
    lines.append(
        f"Overall status: {result.summary.status.upper()} "
        f"(score {result.summary.overall_score:.1f})"
    )
    lines.append(
        f"Total violations: {result.summary.violations_total} "
        f"({summarize_status(result.summary.violations_by_severity)})"
    )
    lines.append("")

    # Detected copper stackup -- so a dropped inner layer is visible, not silent.
    if result.design.layers:
        lines.append(f"Detected copper stackup ({result.design.stackup_layers} layers):")
        for ly in result.design.layers:
            lines.append(f"  - {ly}")
        lines.append("")
    if result.warnings:
        lines.append("WARNINGS:")
        for w in result.warnings:
            lines.append(f"  ! {w}")
        lines.append("")

    for cat in result.categories:
        lines.append(
            f"[{cat.category_id}] {cat.name or ''} - "
            f"status: {cat.status or 'n/a'}, "
            f"score: {cat.score if cat.score is not None else 'n/a'}, "
            f"violations: {cat.violations_count}"
        )
        for check in cat.checks:
            tag = " [heuristic]" if check.confidence == "heuristic" else ""
            lines.append(f"  - {check.check_id}: {check.status} ({check.severity}){tag}")
            if check.metric and check.metric.measured_value is not None:
                mv = check.metric.measured_value
                units = check.metric.units or ""
                lines.append(f"      measured: {mv} {units}")
            if check.violations:
                first = check.violations[0]
                lines.append(f"      first violation: {first.message}")
            if check.status in ("fail", "warning"):
                rem = remediation_for(check.check_id)
                if rem is not None:
                    lines.append(f"      fix: {rem.fix}")
                    lines.append(f"      impact: {rem.impact}")
        lines.append("")

    return "\n".join(lines)


def generate_markdown_report(result: DfmResult) -> str:
    lines = []

    lines.append(f"# DFM report - {result.design.name}")
    if result.design.revision:
        lines.append(f"_Revision: {result.design.revision}_")
    lines.append("")
    lines.append(f"- Ruleset: **{result.ruleset.name} {result.ruleset.version}**")
    lines.append(
        f"- Overall status: **{result.summary.status.upper()}** "
        f"(score **{result.summary.overall_score:.1f}**)"
    )
    lines.append(
        f"- Total violations: **{result.summary.violations_total}** "
        f"({summarize_status(result.summary.violations_by_severity)})"
    )
    lines.append("")

    for cat in result.categories:
        lines.append(f"## {cat.name or cat.category_id}")
        lines.append("")
        lines.append(
            f"- Category id: `{cat.category_id}`  \n"
            f"- Status: **{cat.status or 'n/a'}**  \n"
            f"- Score: **{cat.score if cat.score is not None else 'n/a'}**  \n"
            f"- Violations: **{cat.violations_count}**"
        )
        lines.append("")
        lines.append("| Check id | Status | Severity | Score | Violations |")
        lines.append("|----------|--------|----------|-------|-----------|")
        for check in cat.checks:
            score = "" if check.score is None else f"{check.score:.1f}"
            lines.append(
                f"| `{check.check_id}` | {check.status} | {check.severity} | "
                f"{score} | {len(check.violations)} |"
            )
        lines.append("")

    # Recommended fixes: one actionable line per failing/warning check.
    fixes = []
    seen = set()
    for cat in result.categories:
        for check in cat.checks:
            if check.status in ("fail", "warning") and check.check_id not in seen:
                rem = remediation_for(check.check_id)
                if rem is not None:
                    seen.add(check.check_id)
                    fixes.append((check.status, check.check_id, rem))
    if fixes:
        fixes.sort(key=lambda t: 0 if t[0] == "fail" else 1)
        lines.append("## Recommended fixes")
        lines.append("")
        for st, cid, rem in fixes:
            mark = "❌" if st == "fail" else "⚠️"
            lines.append(f"- {mark} **`{cid}`** — {rem.fix} _(impact: {rem.impact})_")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-contained HTML report (board render + violation overlays)
# ---------------------------------------------------------------------------

_SEV_COLOR = {
    "critical": "#e5484d", "error": "#e5484d",
    "warning": "#f5a623", "info": "#4c8dff",
}
_STATUS_COLOR = {
    "pass": "#30a46c", "warning": "#f5a623",
    "fail": "#e5484d", "not_applicable": "#8b8b8b",
}
# fill / stroke per layer type for the board render
_LAYER_STYLE = {
    "copper": ("#c98a3b", None, 0.55),
    "mask": ("#1f6b47", None, 0.30),
    "silkscreen": ("#e8e8e8", None, 0.55),
    "outline": (None, "#9fb3c8", 1.0),
    "mechanical": (None, "#9fb3c8", 0.8),
}


def _esc(s) -> str:
    return _html.escape("" if s is None else str(s))


def _render_board_svg(geometry, located) -> str:
    """SVG of the board polygons with a marker for each located violation.
    ``located`` is a list of (global_index, violation). Returns "" if there is
    no drawable geometry."""
    if geometry is None:
        return ""
    b = geometry.board_bounds()
    if b is None:
        return ""
    minx, miny, maxx, maxy = b.min_x, b.min_y, b.max_x, b.max_y
    w, h = maxx - minx, maxy - miny
    if w <= 0 or h <= 0:
        return ""
    pad = max(w, h) * 0.04
    flip = miny + maxy  # svg_y = flip - y  (PCB Y-up -> SVG Y-down)

    parts = [
        f'<svg viewBox="{minx - pad:.3f} {miny - pad:.3f} {w + 2 * pad:.3f} '
        f'{h + 2 * pad:.3f}" class="board" xmlns="http://www.w3.org/2000/svg">',
        f'<rect x="{minx:.3f}" y="{miny:.3f}" width="{w:.3f}" height="{h:.3f}" '
        f'class="board-bg"/>',
    ]

    # Draw non-outline fills first, outline strokes last (on top).
    order = ["copper", "mask", "silkscreen", "mechanical", "outline"]
    for lt in order:
        fill, stroke, opacity = _LAYER_STYLE.get(lt, (None, "#888", 0.5))
        for layer in geometry.get_layers_by_type(lt):
            for poly in layer.polygons:
                pts = " ".join(
                    f"{v.x:.3f},{flip - v.y:.3f}" for v in poly.vertices)
                if not pts:
                    continue
                attrs = f'opacity="{opacity}"'
                attrs += f' fill="{fill}"' if fill else ' fill="none"'
                if stroke:
                    attrs += f' stroke="{stroke}" stroke-width="{max(w, h) * 0.004:.3f}"'
                parts.append(f'<polygon points="{pts}" {attrs}/>')

    r = max(w, h) * 0.014
    for gi, v in located:
        loc = v.location
        x = loc.x_mm
        y = flip - loc.y_mm
        color = _SEV_COLOR.get(v.severity, "#4c8dff")
        parts.append(
            f'<g class="marker" id="m{gi}">'
            f'<circle class="halo" cx="{x:.3f}" cy="{y:.3f}" r="{r * 2.4:.3f}" '
            f'fill="none" stroke="{color}"/>'
            f'<circle cx="{x:.3f}" cy="{y:.3f}" r="{r:.3f}" fill="{color}" '
            f'stroke="#fff" stroke-width="{r * 0.25:.3f}">'
            f'<title>{_esc(v.message)}</title></circle></g>')
    parts.append("</svg>")
    return "".join(parts)


def generate_html_report(result: DfmResult, geometry: Optional[object] = None) -> str:
    """Render a single self-contained HTML report: summary, the board with
    violation markers overlaid, and per-category findings cross-linked to the
    markers. No external assets (safe to open locally or archive)."""
    s = result.summary
    status = s.status
    status_color = _STATUS_COLOR.get(status, "#8b8b8b")

    # Assign a stable global index to every violation that has coordinates.
    located = []
    marker_of = {}  # id(violation) -> global index
    gi = 0
    for cat in result.categories:
        for chk in cat.checks:
            for v in chk.violations:
                loc = v.location
                if loc is not None and loc.x_mm is not None and loc.y_mm is not None:
                    located.append((gi, v))
                    marker_of[id(v)] = gi
                    gi += 1

    svg = _render_board_svg(geometry, located)

    counts = s.violations_by_severity
    out = []
    out.append("<!doctype html><html lang='en'><head><meta charset='utf-8'>")
    out.append("<meta name='viewport' content='width=device-width, initial-scale=1'>")
    out.append(f"<title>DFM report - {_esc(result.design.name)}</title>")
    out.append("<style>" + _REPORT_CSS + "</style></head><body>")

    # Header / summary
    out.append("<header class='summary'>")
    out.append(f"<h1>DFM report <span class='dim'>{_esc(result.design.name)}</span></h1>")
    out.append(
        f"<div class='badges'>"
        f"<span class='status' style='background:{status_color}'>{_esc(status.upper())}</span>"
        f"<span class='score'>score {s.overall_score:.0f}</span>"
        f"<span class='muted'>ruleset {_esc(result.ruleset.name)} {_esc(result.ruleset.version)}</span>"
        f"</div>")
    out.append(
        f"<div class='counts'>{s.violations_total} violations &middot; "
        f"<b class='sev-error'>{counts.error} error</b> &middot; "
        f"<b class='sev-warning'>{counts.warning} warning</b> &middot; "
        f"<b class='sev-info'>{counts.info} info</b> &middot; "
        f"<b>{counts.critical} critical</b></div>")
    if result.design.layers:
        chips = "".join(f"<span class='chip'>{_esc(ly)}</span>" for ly in result.design.layers)
        out.append(f"<div class='stackup'><b>Copper stackup ({result.design.stackup_layers}):</b> {chips}</div>")
    for w in result.warnings:
        out.append(f"<div class='warn'>&#9888; {_esc(w)}</div>")
    out.append("</header>")

    out.append("<div class='layout'>")

    # Board panel
    out.append("<div class='board-panel'>")
    if svg:
        out.append(svg)
        out.append("<p class='hint'>Click a &#128205; finding to highlight it on the board.</p>")
    else:
        out.append("<p class='hint'>No drawable board geometry available.</p>")
    out.append("</div>")

    # Findings panel
    out.append("<div class='findings'>")
    for cat in result.categories:
        cstatus = cat.status or "n/a"
        ccolor = _STATUS_COLOR.get(cat.status or "", "#8b8b8b")
        out.append(
            f"<h2><span class='dot' style='background:{ccolor}'></span>"
            f"{_esc(cat.name or cat.category_id)} "
            f"<span class='muted'>{_esc(cstatus)} &middot; {cat.violations_count} viol.</span></h2>")
        for chk in cat.checks:
            scolor = _SEV_COLOR.get(chk.severity or "info", "#4c8dff")
            measured = ""
            if chk.metric and chk.metric.measured_value is not None:
                measured = f"<span class='measured'>{chk.metric.measured_value} {_esc(chk.metric.units or '')}</span>"
            heur = "<span class='heur' title='heuristic check — treat as a checklist, not a hard gate'>heuristic</span>" if chk.confidence == "heuristic" else ""
            out.append(
                f"<div class='check'><div class='check-head'>"
                f"<span class='sev' style='background:{scolor}'></span>"
                f"<code>{_esc(chk.check_id)}</code> "
                f"<span class='st'>{_esc(chk.status)}</span> {heur} {measured}</div>")
            for v in chk.violations:
                mi = marker_of.get(id(v))
                pin = ""
                if mi is not None:
                    pin = (f"<button class='pin' onclick=\"hl('m{mi}')\" "
                           f"title='highlight on board'>&#128205;</button>")
                out.append(
                    f"<div class='viol sev-{_esc(v.severity)}'>{pin}"
                    f"<span>{_esc(v.message)}</span></div>")
            if chk.status in ("fail", "warning"):
                rem = remediation_for(chk.check_id)
                if rem is not None:
                    out.append(
                        f"<div class='fix'><strong>Fix:</strong> {_esc(rem.fix)} "
                        f"<span class='muted'>(impact: {_esc(rem.impact)})</span></div>")
            out.append("</div>")
    out.append("</div>")  # findings

    out.append("</div>")  # layout
    out.append("<script>" + _REPORT_JS + "</script>")
    out.append("</body></html>")
    return "".join(out)


_REPORT_CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--muted:#6b7280;--card:#f6f7f9;--border:#e5e7eb;}
@media (prefers-color-scheme:dark){:root{--bg:#0e1116;--fg:#e6e6e6;--muted:#9aa4b2;--card:#171b22;--border:#2a2f3a;}}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--fg);}
.summary{padding:18px 22px;border-bottom:1px solid var(--border);}
h1{margin:0 0 8px;font-size:22px;font-weight:700}
h1 .dim{color:var(--muted);font-weight:500}
.badges{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.status{color:#fff;font-weight:700;padding:3px 12px;border-radius:6px;font-size:14px}
.score{font-weight:600}.muted{color:var(--muted);font-size:13px}
.counts{margin-top:8px;color:var(--muted);font-size:13px}
.stackup{margin-top:8px;font-size:12px;color:var(--muted);display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.chip{background:var(--card);border:1px solid var(--border);border-radius:5px;padding:1px 7px;font-family:ui-monospace,Menlo,monospace}
.warn{margin-top:8px;padding:6px 10px;border-radius:6px;background:#fdecc8;color:#7a4f01;font-size:13px}
@media (prefers-color-scheme:dark){.warn{background:#3a2d0a;color:#f5cf6b}}
.sev-error{color:#e5484d}.sev-warning{color:#f5a623}.sev-info{color:#4c8dff}
.layout{display:flex;gap:0;align-items:flex-start}
.board-panel{flex:1 1 55%;position:sticky;top:0;padding:16px;min-width:0}
.findings{flex:1 1 45%;padding:16px 20px;max-height:100vh;overflow:auto}
svg.board{width:100%;height:auto;max-height:82vh;background:#08130d;border-radius:10px;border:1px solid var(--border)}
.board-bg{fill:#0c2a1c}
.marker .halo{opacity:0;stroke-width:2;transition:opacity .2s}
.marker.hl .halo{opacity:.95;animation:pulse 1s ease-out 2}
@keyframes pulse{0%{opacity:.2}50%{opacity:1}100%{opacity:.4}}
.hint{color:var(--muted);font-size:12px;margin:8px 2px}
h2{font-size:15px;margin:18px 0 8px;display:flex;align-items:center;gap:8px}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block}
.check{border:1px solid var(--border);border-radius:8px;padding:8px 10px;margin:6px 0;background:var(--card)}
.check-head{display:flex;align-items:center;gap:8px;font-size:13px}
.sev{width:8px;height:8px;border-radius:50%;display:inline-block;flex:none}
.st{color:var(--muted);text-transform:uppercase;font-size:11px;letter-spacing:.03em}
.heur{font-size:10px;color:#8a6d00;background:#fdecc8;border-radius:4px;padding:0 5px;text-transform:uppercase;letter-spacing:.03em}
@media (prefers-color-scheme:dark){.heur{background:#3a2d0a;color:#f5cf6b}}
.measured{margin-left:auto;color:var(--muted);font-size:12px}
code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
.viol{display:flex;gap:8px;align-items:flex-start;font-size:12.5px;color:var(--muted);padding:4px 0 2px 16px}
.viol.sev-error{color:#e5484d}.viol.sev-warning{color:#c8861a}
.fix{font-size:12.5px;padding:3px 0 2px 16px;line-height:1.4}
.pin{border:none;background:none;cursor:pointer;font-size:13px;padding:0;line-height:1.2}
@media(max-width:820px){.layout{flex-direction:column}.board-panel{position:static;width:100%}.findings{max-height:none}}
"""

_REPORT_JS = """
function hl(id){
  document.querySelectorAll('.marker.hl').forEach(function(m){m.classList.remove('hl')});
  var el=document.getElementById(id);
  if(el){el.classList.add('hl');el.parentNode.appendChild(el);el.scrollIntoView({block:'nearest'});}
}
"""


def generate_pr_summary(result: DfmResult, top: int = 10) -> str:
    """A compact Markdown summary of a DFM run, suitable for a PR comment or a
    CI job summary: overall status/score, counts, and the top failing/warning
    checks."""
    s = result.summary
    icon = {"pass": "✅", "warning": "⚠️", "fail": "❌"}.get(s.status, "•")
    c = s.violations_by_severity
    lines = [
        f"### {icon} PCB DFM — {s.status.upper()} (score {s.overall_score:.0f})",
        (f"Ruleset `{_esc_md(result.ruleset.name)}` · {s.violations_total} violations "
         f"({c.error} error, {c.warning} warning, {c.info} info)"),
        "",
    ]
    if result.design.layers:
        stack = " → ".join(_esc_md(ly.split(":")[0]) for ly in result.design.layers)
        lines.append(f"Detected copper stackup ({result.design.stackup_layers}): {stack}")
    for w in result.warnings:
        lines.append(f"> ⚠️ {_esc_md(w)}")
    if result.design.layers or result.warnings:
        lines.append("")

    items = []
    for cat in result.categories:
        for chk in cat.checks:
            if chk.status in ("fail", "warning"):
                msg = chk.violations[0].message if chk.violations else ""
                heur = chk.confidence == "heuristic"
                items.append((0 if chk.status == "fail" else 1, chk.status, chk.check_id, msg, heur))
    items.sort(key=lambda t: t[0])

    if items:
        lines.append("**Needs attention**")
        for _rank, st, cid, msg, heur in items[:top]:
            mark = "❌" if st == "fail" else "⚠️"
            msg = msg if len(msg) <= 160 else msg[:157] + "…"
            tag = " _(heuristic)_" if heur else ""
            lines.append(f"- {mark} `{_esc_md(cid)}`{tag} — {_esc_md(msg)}")
            rem = remediation_for(cid)
            if rem is not None:
                lines.append(f"  - **Fix:** {_esc_md(rem.fix)} _(impact: {_esc_md(rem.impact)})_")
        if len(items) > top:
            lines.append(f"- …and {len(items) - top} more")
    else:
        lines.append("No blocking findings. \U0001f389")

    lines += ["", "<sub>generated by pcb-dfm</sub>"]
    return "\n".join(lines)


def _esc_md(s) -> str:
    # Neutralize characters that would break a Markdown table/inline-code cell.
    return ("" if s is None else str(s)).replace("|", "\\|").replace("`", "'")
