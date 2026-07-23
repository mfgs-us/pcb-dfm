from __future__ import annotations

from pathlib import Path
from typing import List

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.gerber_backend import GERBONARA_AVAILABLE, excellon_hits_mm
from ..ingest import GerberFileInfo
from ..results import CheckResult, MetricResult, Violation, ViolationLocation

# Reuse the same drill parser used by via_in_pad_thermal_balance

def _resolve_limit(check_def, key: str, default):
    """Resolve a threshold, preferring the pre-normalized ``check_def.limits``
    block; fall back to this check's ``metric.target``/``metric.limits`` when
    that plumbing is absent (count units are unscaled), so JSON thresholds are
    honored either way. Non-numeric (e.g. legacy boolean) bounds are ignored."""
    lim = getattr(check_def, "limits", None) or {}
    v = lim.get(key)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)

    metric = getattr(check_def, "metric", None) or {}
    units = str(metric.get("units", "count")).lower()
    scale = 0.001 if units in ("um", "µm", "micron", "microns") else 1.0
    mapping = {
        "recommended_min": ("target", "min"),
        "recommended_max": ("target", "max"),
        "absolute_min": ("limits", "min"),
        "absolute_max": ("limits", "max"),
    }
    node_key, sub = mapping.get(key, (None, None))
    if node_key is not None:
        node = metric.get(node_key)
        if isinstance(node, dict):
            nv = node.get(sub)
            if isinstance(nv, (int, float)) and not isinstance(nv, bool):
                return float(nv) * scale
    return default


def _extract_drill_hits_mm(path: str) -> List[dict]:
    """Drill hits in mm, via the gerbonara parse backend (#3).

    Returns dicts with ``x_mm`` / ``y_mm`` / ``diameter_mm``. The pcb-tools path
    chained to_metric()/to_inch() and re-scaled by 25.4, which double-converted
    mm-native drill files.
    """
    return [
        {"x_mm": h.x_mm, "y_mm": h.y_mm, "diameter_mm": h.diameter_mm}
        for h in excellon_hits_mm(Path(path))
    ]
@register_check("missing_tooling_holes")
def run_missing_tooling_holes(ctx: CheckContext) -> CheckResult:
    """
    Check for the presence of reasonably sized tooling holes near the board edges.

    ADVISORY check. Tooling holes are frequently added at the PANEL level rather
    than on the individual board, so a bare board with no tooling holes is a
    perfectly normal, fabricable design. This check therefore never hard-fails:
    the absence of tooling holes is reported as a WARNING (informational nudge),
    and a board with no drills at all is not_applicable.

    Heuristic:
      - Use drill files from ingest (layer_type == "drill").
      - Consider drills with diameter within [min_tool_d_mm, max_tool_d_mm] as tooling candidates.
      - A tooling hole is "near edge" if it is within edge_margin_mm of at least one board edge.
      - We count distinct candidate holes; metric is the count.
      - Status:
          pass:    count >= recommended_min
          warning: count <  recommended_min  (advisory only, incl. count == 0)
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", "count")

    # Count thresholds come from the plumbed limits (target -> recommended,
    # limits -> absolute), with a metric fallback. Guard against non-numeric
    # (e.g. legacy boolean) metric bounds collapsing the thresholds to 1/1.
    #
    # Expected number of tooling holes for a "pass"; below this is an advisory
    # warning only. Default 2 (a typical diagonal pair).
    recommended_min = _resolve_limit(ctx.check_def, "recommended_min", 2.0)

    raw_cfg = getattr(ctx.check_def, "raw", None) or {}
    min_tool_d_mm = float(raw_cfg.get("min_tool_d_mm", 2.0))   # ~80 mil
    max_tool_d_mm = float(raw_cfg.get("max_tool_d_mm", 5.0))   # ~200 mil
    edge_margin_mm = float(raw_cfg.get("edge_margin_mm", 5.0)) # distance from edge to count as "edge"

    # Collect drills from ingest (same approach as the working drill checks)
    drill_files: List[GerberFileInfo] = [
        f for f in ctx.ingest.files if f.layer_type == "drill"
    ]

    hits: List[dict] = []
    if GERBONARA_AVAILABLE:
        for info in drill_files:
            hits.extend(_extract_drill_hits_mm(str(info.path)))

    if not hits:
        # Genuinely no drills to inspect -> honestly not applicable.
        viol = Violation(
            severity="info",
            message="No drill files or drill hits found; tooling-hole check not applicable.",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="not_applicable",
            severity="info",
            score=100.0,
            metric=MetricResult(
                kind="count",
                units=units,
                measured_value=None,
                target=recommended_min,
                limit_low=None,
                limit_high=None,
                margin_to_limit=None,
            ),
            violations=[viol],
        ).finalize()

    # Board extent from the real geometry API (Bounds or None).
    bounds = ctx.geometry.board_bounds()
    if bounds is None:
        viol = Violation(
            severity="warning",
            message="Board extent not available; cannot reliably detect tooling holes.",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="warning",
            severity="info",  # Default value, will be overridden by finalize()
            score=50.0,
            metric=MetricResult(
                kind="count",
                units=units,
                measured_value=None,
                target=recommended_min,
                limit_low=None,
                limit_high=None,
                margin_to_limit=None,
            ),
            violations=[viol],
        ).finalize()

    min_x = float(bounds.min_x)
    max_x = float(bounds.max_x)
    min_y = float(bounds.min_y)
    max_y = float(bounds.max_y)

    # Filter candidate tooling holes
    tooling_hits: List[dict] = []
    for h in hits:
        d = float(h["diameter_mm"])
        if d < min_tool_d_mm or d > max_tool_d_mm:
            continue

        x = float(h["x_mm"])
        y = float(h["y_mm"])
        # distance to nearest edge
        dist_edge = min(x - min_x, max_x - x, y - min_y, max_y - y)
        if dist_edge <= edge_margin_mm:
            tooling_hits.append(h)

    count = float(len(tooling_hits))

    # Status -- ADVISORY ONLY: never a hard fail. A board with too few (or zero)
    # tooling holes is flagged as a warning because tooling is frequently added
    # at the panel level rather than on the individual board.
    if count >= recommended_min:
        status = "pass"
        sev = "info"
        score = 100.0
    elif count == 0:
        # No tooling holes at all is the NORMAL case for a bare board -- tooling
        # is added when the board is panelized, so there is nothing here to
        # assess. Warning on it flagged every unpanelized design, which is most
        # of them. Some-but-too-few is different: that placement looks
        # incomplete, and is still worth an advisory.
        status = "not_applicable"
        sev = "info"
        score = None
    else:
        status = "warning"
        sev = "warning"
        # scale from ~60 (none) up toward 100 as we approach the recommendation
        if recommended_min > 0:
            score = max(0.0, min(100.0, 60.0 + 40.0 * (count / recommended_min)))
        else:
            score = 60.0

    margin_to_limit = count - recommended_min

    violations: List[Violation] = []
    if status != "pass":
        if count == 0:
            msg = (
                f"No candidate tooling holes detected on the board (recommended >= "
                f"{recommended_min:.0f}). This is advisory only: tooling holes are "
                f"often added at the panel level, so a tooling-hole-free bare board "
                f"is normal. Confirm panelization with your fab."
            )
        else:
            msg = (
                f"Only {int(count)} candidate tooling hole(s) detected "
                f"(recommended >= {recommended_min:.0f}). Advisory only -- panels "
                f"frequently add tooling holes at the panel level."
            )

        # Use board center as a generic location hint
        cx = min_x + 0.5 * (max_x - min_x)
        cy = min_y + 0.5 * (max_y - min_y)

        violations.append(
            Violation(
                severity=sev,
                message=msg,
                location=ViolationLocation(
                    layer=None,
                    x_mm=cx,
                    y_mm=cy,
                    width_mm=max_x - min_x,
                    height_mm=max_y - min_y,
                    net=None,
                    component=None,
                ),
            )
        )
    else:
        msg = (
            f"Detected {int(count)} candidate tooling hole(s) near board edges; "
            f"recommended >= {recommended_min:.0f}."
        )
        violations.append(
            Violation(
                severity="info",
                message=msg,
                location=None,
            )
        )

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        severity="info",  # Default value, will be overridden by finalize()
        status=status,
        score=score,
        metric=MetricResult(
            kind="count",
            units=units,
            measured_value=count,
            target=recommended_min,
            limit_low=None,
            limit_high=None,
            margin_to_limit=margin_to_limit,
        ),
        violations=violations,
    ).finalize()
