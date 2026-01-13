from __future__ import annotations

from typing import List, Optional

from ..results import CheckResult, Violation, MetricResult
from ..engine.context import CheckContext
from ..engine.check_runner import register_check
from ..ingest import GerberFileInfo

try:
    import gerber
except Exception:
    gerber = None

_INCH_TO_MM = 25.4


@register_check("drill_aspect_ratio")
def run_drill_aspect_ratio(ctx: CheckContext) -> CheckResult:
    """
    Compute drill aspect ratio:

        aspect_ratio = board_thickness_mm / min_drill_diameter_mm

    Limits are typically expressed as max allowed aspect ratio.
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", metric_cfg.get("unit", ""))

    limits = ctx.check_def.limits or {}
    recommended_max = float(limits.get("recommended_max", 8.0))
    absolute_max = float(limits.get("absolute_max", 10.0))

    # Board thickness: for now, allow override via check_def.raw, else default 1.6 mm
    board_thickness_mm = float(
        ctx.check_def.raw.get("board_thickness_mm", 1.6)
    )

    drill_files: List[GerberFileInfo] = [
        f for f in ctx.ingest.files if f.layer_type == "drill"
    ]

    if gerber is None or not drill_files:
        viol = Violation(
            severity="warning",
            message="No drill parser available or no drill files found to compute drill aspect ratio.",
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
                kind="ratio",
                units="%",
                measured_value=None,
                target=recommended_max,
                limit_low=None,
                limit_high=absolute_max,
                margin_to_limit=None,
            ),
            violations=[viol],
        ).finalize()

    diameters_mm: List[float] = []
    for info in drill_files:
        diameters_mm.extend(_extract_tool_diameters_mm(info.path))

    if not diameters_mm:
        viol = Violation(
            severity="warning",
            message="No drill tools or hits found to compute drill aspect ratio.",
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
                kind="ratio",
                units="%",
                measured_value=None,
                target=recommended_max,
                limit_low=None,
                limit_high=absolute_max,
                margin_to_limit=None,
            ),
            violations=[viol],
        ).finalize()

    min_d_mm = min(diameters_mm)
    aspect = board_thickness_mm / min_d_mm

    # Decide status only (severity handled by finalize)
    if aspect > absolute_max:
        status = "fail"
    elif aspect > recommended_max:
        status = "warning"
    else:
        status = "pass"

    # Score: 100 at <= recommended_max, 0 at >= absolute_max
    if aspect <= recommended_max:
        score = 100.0
    elif aspect >= absolute_max:
        score = 0.0
    else:
        span = absolute_max - recommended_max
        score = max(0.0, min(100.0, 100.0 * (absolute_max - aspect) / span))

    margin_to_limit = float(absolute_max - aspect)

    violations: List[Violation] = []
    if status != "pass":
        msg = (
            f"Drill aspect ratio {aspect:.2f}:1 exceeds recommended {recommended_max:.2f}:1 "
            f"(absolute maximum {absolute_max:.2f}:1)."
        )
        violations.append(
            Violation(
                severity=severity,
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
            kind="ratio",
            units="%",
            measured_value=float(aspect),
            target=recommended_max,
            limit_low=None,
            limit_high=absolute_max,
            margin_to_limit=margin_to_limit,
        ),
        violations=violations,
    ).finalize()


def _extract_tool_diameters_mm(path) -> List[float]:
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

    diameters_inch: List[float] = []

    tools = getattr(drill_layer, "tools", None)
    if isinstance(tools, dict):
        for tool in tools.values():
            d = getattr(tool, "diameter", None)
            if d is None:
                d = getattr(tool, "size", None)
            if d is not None:
                try:
                    diameters_inch.append(float(d))
                except Exception:
                    continue

    hits = getattr(drill_layer, "hits", None)
    if hits is not None:
        for hit in hits:
            try:
                tool = getattr(hit, "tool", None)
                d = getattr(tool, "diameter", None) if tool is not None else None
                if d is not None:
                    diameters_inch.append(float(d))
                    continue
            except Exception:
                pass
            try:
                tool, _pos = hit
                d = getattr(tool, "diameter", None)
                if d is not None:
                    diameters_inch.append(float(d))
            except Exception:
                continue

    return [d * _INCH_TO_MM for d in diameters_inch]
