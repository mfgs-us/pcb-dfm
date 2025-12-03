from __future__ import annotations

import math
from typing import List, Optional

from ..results import CheckResult, Violation, ViolationLocation
from ..engine.context import CheckContext
from ..engine.check_runner import register_check
from ..ingest import GerberFileInfo

# Reuse the same drill parser used by via_in_pad_thermal_balance
try:
    import gerber
except Exception:
    gerber = None


def _extract_drill_hits_mm(path: str) -> List[dict]:
    """
    Use gerber.read on a drill file and extract hits as mm.

    Returns list of dicts with:
      - x_mm
      - y_mm
      - diameter_mm
    """
    if gerber is None:
        return []

    try:
        drill_layer = gerber.read(path)
    except Exception:
        return []

    try:
        drill_layer.to_metric()
        inch_to_mm = 25.4  # fallback if to_metric is a no-op
        units = getattr(drill_layer, "units", "").lower()
        if units == "mm":
            inch_to_mm = 25.4  # we still treat internal conversions via inch scaling below
    except Exception:
        try:
            drill_layer.to_inch()
        except Exception:
            pass

    # We will always go via inch to mm, as in other drill based checks
    try:
        drill_layer.to_inch()
    except Exception:
        pass

    INCH_TO_MM = 25.4

    hits_out: List[dict] = []
    hits = getattr(drill_layer, "hits", None)
    if hits is None:
        return hits_out

    for hit in hits:
        x_in = y_in = None
        d_in = None

        # New style object API
        try:
            if hasattr(hit, "x") and hasattr(hit, "y"):
                x_in = float(hit.x)
                y_in = float(hit.y)
            elif hasattr(hit, "position"):
                px, py = hit.position  # type: ignore[attr-defined]
                x_in = float(px)
                y_in = float(py)

            tool = getattr(hit, "tool", None)
            if tool is not None:
                d_in = getattr(tool, "diameter", None)
                if d_in is None:
                    d_in = getattr(tool, "size", None)
        except Exception:
            x_in = y_in = d_in = None

        # Older tuple API: (tool, (x, y))
        if x_in is None or y_in is None or d_in is None:
            try:
                tool, (px, py) = hit  # type: ignore[misc]
                x_in = float(px)
                y_in = float(py)
                d_in = getattr(tool, "diameter", None)
                if d_in is None:
                    d_in = getattr(tool, "size", None)
            except Exception:
                continue

        try:
            d_in_float = float(d_in)
        except Exception:
            continue

        hits_out.append(
            {
                "x_mm": x_in * INCH_TO_MM,
                "y_mm": y_in * INCH_TO_MM,
                "diameter_mm": d_in_float * INCH_TO_MM,
            }
        )

    return hits_out


@register_check("missing_tooling_holes")
def run_missing_tooling_holes(ctx: CheckContext) -> CheckResult:
    """
    Check for the presence of reasonably sized tooling holes near the board edges.

    Heuristic:
      - Use drill files from ingest (layer_type == "drill").
      - Consider drills with diameter within [min_tool_d_mm, max_tool_d_mm] as tooling candidates.
      - A tooling hole is "near edge" if it is within edge_margin_mm of at least one board edge.
      - We count distinct candidate holes; metric is the count.
      - Status:
          pass:   count >= recommended_min
          warn:   absolute_min <= count < recommended_min
          fail:   count < absolute_min
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", "count")
    target_cfg = metric_cfg.get("target", {}) or {}
    limits_cfg = metric_cfg.get("limits", {}) or {}

    # Recommended/absolute minima from metric.targets/limits if present, else defaults
    recommended_min = float(target_cfg.get("min", 2.0)) if isinstance(target_cfg, dict) else float(target_cfg or 2.0)
    absolute_min = float(limits_cfg.get("min", 1.0)) if isinstance(limits_cfg, dict) else float(limits_cfg or 1.0)

    raw_cfg = getattr(ctx.check_def, "raw", None) or {}
    min_tool_d_mm = float(raw_cfg.get("min_tool_d_mm", 2.0))   # ~80 mil
    max_tool_d_mm = float(raw_cfg.get("max_tool_d_mm", 5.0))   # ~200 mil
    edge_margin_mm = float(raw_cfg.get("edge_margin_mm", 5.0)) # distance from edge to count as "edge"

    # Board geometry
    board = getattr(ctx.geometry, "board", None)
    if board is None:
        viol = Violation(
            severity="warning",
            message="Board outline/geometry not available; cannot reliably detect tooling holes.",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            severity=ctx.check_def.severity or ctx.check_def.severity_default,
            status="warning",
            score=50.0,
            metric={
                "kind": "count",
                "units": units,
                "measured_value": None,
                "target": recommended_min,
                "limit_low": absolute_min,
                "limit_high": None,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    min_x = float(getattr(board, "min_x_mm", getattr(board, "x_min_mm", 0.0)))
    max_x = min_x + float(getattr(board, "width_mm", getattr(board, "width", 0.0)))
    min_y = float(getattr(board, "min_y_mm", getattr(board, "y_min_mm", 0.0)))
    max_y = min_y + float(getattr(board, "height_mm", getattr(board, "height", 0.0)))

    # Collect drills from ingest
    drill_files: List[GerberFileInfo] = [
        f for f in ctx.ingest.files if f.layer_type == "drill"
    ]

    hits: List[dict] = []
    if gerber is not None:
        for info in drill_files:
            hits.extend(_extract_drill_hits_mm(str(info.path)))

    if not hits:
        viol = Violation(
            severity="warning",
            message="No drill files or drill hits found; cannot check for tooling holes.",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            severity=ctx.check_def.severity or ctx.check_def.severity_default,
            status="warning",
            score=50.0,
            metric={
                "kind": "count",
                "units": units,
                "measured_value": 0.0,
                "target": recommended_min,
                "limit_low": absolute_min,
                "limit_high": None,
                "margin_to_limit": -absolute_min,
            },
            violations=[viol],
        )

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

    # Status
    if count < absolute_min:
        status = "fail"
        sev = "error"
        score = 0.0
    elif count < recommended_min:
        status = "warning"
        sev = "warning"
        # simple linear scale between abs and recommended
        if recommended_min > absolute_min:
            score = max(0.0, min(100.0, 60.0 + 40.0 * (count - absolute_min) / (recommended_min - absolute_min)))
        else:
            score = 60.0
    else:
        status = "pass"
        sev = ctx.check_def.severity or ctx.check_def.severity_default
        score = 100.0

    margin_to_limit = count - absolute_min

    violations: List[Violation] = []
    if status != "pass":
        if count == 0:
            msg = (
                f"No candidate tooling holes detected (recommended >= {recommended_min:.0f}, "
                f"absolute minimum >= {absolute_min:.0f})."
            )
        else:
            msg = (
                f"Only {int(count)} candidate tooling hole(s) detected "
                f"(recommended >= {recommended_min:.0f}, absolute minimum >= {absolute_min:.0f})."
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
        severity=ctx.check_def.severity or ctx.check_def.severity_default,
        status=status,
        score=score,
        metric={
            "kind": "count",
            "units": units,
            "measured_value": count,
            "target": recommended_min,
            "limit_low": absolute_min,
            "limit_high": None,
            "margin_to_limit": margin_to_limit,
        },
        violations=violations,
    )
