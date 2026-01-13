from __future__ import annotations

from pathlib import Path
from typing import List

from ..engine.context import CheckContext
from ..engine.check_runner import register_check
from ..results import CheckResult, Violation, MetricResult
from ..ingest import GerberFileInfo

try:
    import gerber
except Exception:  # pragma: no cover
    gerber = None


_INCH_TO_MM = 25.4


@register_check("drill_wander_budget")
def run_drill_wander_budget(ctx: CheckContext) -> CheckResult:
    """
    Estimate drill wander budget via drill aspect ratio:

        aspect_ratio = board_thickness_mm / min_drill_diameter_mm

    High aspect ratio -> long, skinny holes -> more sensitive to wander and
    layer registration error.

    Limits from check_def.limits (with defaults):
      - recommended_max_aspect (default 8.0)
      - absolute_max_aspect (default 10.0)
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", metric_cfg.get("unit", ":1"))

    limits = ctx.check_def.limits or {}
    recommended_max = float(limits.get("recommended_max_aspect", 8.0))
    absolute_max = float(limits.get("absolute_max_aspect", 10.0))

    # Board thickness (mm) from geometry if available, else default 1.6 mm
    thickness_mm = 1.6
    board = getattr(ctx.geometry, "board", None)
    if board is not None:
        # support thickness_mm or thickness as mm
        t_mm = getattr(board, "thickness_mm", None)
        if t_mm is None:
            t_mm = getattr(board, "thickness", None)
        if t_mm is not None:
            try:
                thickness_mm = float(t_mm)
            except Exception:
                pass

    # Collect drill diameters from drill files
    drill_files: List[GerberFileInfo] = [
        f for f in ctx.ingest.files if f.layer_type == "drill"
    ]

    if gerber is None or not drill_files:
        msg = (
            "No drill parser available or no drill files found; "
            "cannot estimate drill wander budget."
        )
        viol = Violation(
            severity="info",
            message=msg,
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="warning",
            severity="info",  # Default value, will be overridden by finalize()
            score=80.0,
            metric=MetricResult(
                kind="ratio",
                units="%",
                measured_value=None,
                target=recommended_max,
                limit_low=None,
                limit_high=absolute_max,
                margin_to_limit=None,
            ),
            violations=[viol],
        ).finalize().finalize()

    diameters_mm: List[float] = []
    for info in drill_files:
        diameters_mm.extend(_extract_diameters_mm(info.path))

    diameters_mm = [d for d in diameters_mm if d > 0.0]

    if not diameters_mm:
        msg = "No valid drill diameters found; cannot estimate drill wander budget."
        viol = Violation(
            severity="info",
            message=msg,
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="warning",
            severity="info",  # Default value, will be overridden by finalize()
            score=80.0,
            metric=MetricResult(
                kind="ratio",
                units="%",
                measured_value=None,
                target=recommended_max,
                limit_low=None,
                limit_high=absolute_max,
                margin_to_limit=None,
            ),
            violations=[viol],
        ).finalize().finalize()

    min_d_mm = min(diameters_mm)
    aspect = thickness_mm / min_d_mm if min_d_mm > 0 else float("inf")

    # Status from aspect ratio vs thresholds
    if aspect <= recommended_max:
        status = "pass"
        severity = ctx.check_def.severity or "error"
    elif aspect <= absolute_max:
        status = "warning"
        severity = "warning"
    else:
        status = "fail"
        severity = "error"

    # Score: 100 at recommended_max or lower, 0 at or beyond absolute_max
    if aspect <= recommended_max:
        score = 100.0
    elif aspect >= absolute_max:
        score = 0.0
    else:
        span = absolute_max - recommended_max
        score = max(
            0.0,
            min(100.0, 100.0 * (absolute_max - aspect) / span),
        )

    margin_to_limit = float(absolute_max - aspect)

    violations: List[Violation] = []
    if status != "pass":
        msg = (
            f"Drill aspect ratio {aspect:.2f}:1 exceeds recommended "
            f"{recommended_max:.2f}:1 (absolute max {absolute_max:.2f}:1). "
            "High aspect ratio reduces drill wander margin and yield."
        )
        violations.append(
            Violation(
                severity=severity,
                message=msg,
                location=None,
                extra={
                    "board_thickness_mm": thickness_mm,
                    "min_drill_diameter_mm": min_d_mm,
                },
            )
        )

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        severity=ctx.check_def.severity,
        status=status,
        score=score,
        metric=MetricResult(
            kind="ratio",
            units="%",
            measured_value=float(aspect),
            target=recommended_max,
            limit_low=None,
            limit_high=absolute_max,
            margin_to_limit=margin_to_limit,
        ),
        violations=violations,
    )


def _extract_diameters_mm(path: Path) -> List[float]:
    """
    Same drill diameter extraction pattern as in min_drill_size, but kept
    local to this module to avoid cross-module coupling.
    """
    if gerber is None:
        return []

    try:
        drill_layer = gerber.read(str(path))
    except Exception:
        return []

    try:
        drill_layer.to_inch()
    except Exception:
        pass

    diam_in: List[float] = []

    tools = getattr(drill_layer, "tools", None)
    if isinstance(tools, dict):
        for tool in tools.values():
            d = getattr(tool, "diameter", None)
            if d is None:
                d = getattr(tool, "size", None)
            if d is not None:
                try:
                    diam_in.append(float(d))
                except Exception:
                    continue

    hits = getattr(drill_layer, "hits", None)
    if hits is not None:
        for hit in hits:
            try:
                tool = getattr(hit, "tool", None)
                if tool is not None and hasattr(tool, "diameter"):
                    d = float(tool.diameter)
                    diam_in.append(d)
                    continue
            except Exception:
                pass
            try:
                tool, _pos = hit
                d = getattr(tool, "diameter", None)
                if d is not None:
                    diam_in.append(float(d))
            except Exception:
                continue

    return [d * _INCH_TO_MM for d in diam_in]
