from __future__ import annotations

from typing import List, Optional
from pathlib import Path

from ..results import CheckResult, Violation, ViolationLocation, MetricResult
from ..engine.context import CheckContext
from ..engine.check_runner import register_check
from ..ingest import GerberFileInfo

# We use pcb-tools via gerber.read, but do not import excellon directly
try:
    import gerber
except Exception:
    gerber = None


_INCH_TO_MM = 25.4


@register_check("min_drill_size")
def run_min_drill_size(ctx: CheckContext) -> CheckResult:
    """
    Compute the minimum drill diameter across all drill files.

    Uses pcb-tools gerber.read on any file classified as layer_type="drill"
    (Excellon .drl etc), normalizes to inch via .to_inch(), then converts
    tool diameters to mm.

    Metric:
      - measured_value: min_drill_diameter_mm

    Status:
      - pass: min >= recommended_min
      - warning: absolute_min <= min < recommended_min
      - fail: min < absolute_min
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", metric_cfg.get("unit", "mm"))

    limits = ctx.check_def.limits or {}
    recommended_min = float(limits.get("recommended_min", 0.2))
    absolute_min = float(limits.get("absolute_min", 0.15))

    # Collect drill file paths from ingest
    drill_files: List[GerberFileInfo] = [
        f for f in ctx.ingest.files if f.layer_type == "drill"
    ]

    if gerber is None or not drill_files:
        # Cannot measure, report as warning with None metric
        message = "No drill parser available or no drill files found to compute minimum drill size."
        viol = Violation(
            severity="warning",
            message=message,
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
                kind="geometry",
                units=units,
                measured_value=None,
                target=recommended_min,
                limit_low=absolute_min,
                limit_high=None,
                margin_to_limit=None,
            ),
            violations=[viol],
        ).finalize()

    diameters_mm: List[float] = []

    for info in drill_files:
        diameters_mm.extend(_extract_tool_diameters_mm(info.path))

    if not diameters_mm:
        message = "No drill tools or hits found to compute minimum drill size."
        viol = Violation(
            severity="warning",
            message=message,
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
                kind="geometry",
                units=units,
                measured_value=None,
                target=recommended_min,
                limit_low=absolute_min,
                limit_high=None,
                margin_to_limit=None,
            ),
            violations=[viol],
        ).finalize()

    min_d_mm = min(diameters_mm)

    # Determine status only (severity handled by finalize)
    if min_d_mm < absolute_min:
        status = "fail"
    elif min_d_mm < recommended_min:
        status = "warning"
    else:
        status = "pass"

    # Score: 0 at absolute_min or below, 100 at recommended_min or above, linear in between
    if min_d_mm >= recommended_min:
        score = 100.0
    elif min_d_mm <= absolute_min:
        score = 0.0
    else:
        span = recommended_min - absolute_min
        score = max(0.0, min(100.0, 100.0 * (min_d_mm - absolute_min) / span))

    margin_to_limit = float(min_d_mm - absolute_min)

    # There is not a single best XY location here (drills are discrete),
    # but we can leave location None or later extend to point to worst hit.
    violations: List[Violation] = []
    if status != "pass":
        message = (
            f"Minimum drill size {min_d_mm:.3f} mm is below "
            f"recommended {recommended_min:.3f} mm (absolute minimum {absolute_min:.3f} mm)."
        )
        violations.append(
            Violation(
                severity=severity,
                message=message,
                location=None,
            )
        )

    # Convert to microns if units are in um
    if units == "um":
        measured = min_d_mm * 1000.0
        target = recommended_min * 1000.0
        limit_low = absolute_min * 1000.0
    else:
        measured = min_d_mm
        target = recommended_min
        limit_low = absolute_min

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",  # Default value, will be overridden by finalize()
        score=score,
        metric=MetricResult(
            kind="geometry",
            units=units,
            measured_value=float(measured),
            target=target,
            limit_low=limit_low,
            limit_high=None,
            margin_to_limit=margin_to_limit * (1000.0 if units == "um" else 1.0),
        ),
        violations=violations,
    ).finalize()


def _extract_tool_diameters_mm(path: Path) -> List[float]:
    """
    Use gerber.read on a drill file and extract tool diameters in mm.

    We are deliberately defensive because pcb-tools Excellon API
    varies between versions.

    Strategy:
      - gerber.read(path) -> drill_layer
      - drill_layer.to_inch()
      - collect diameters from:
          - drill_layer.tools[*].diameter or .size
          - hits[*].tool.diameter as fallback
    """
    if gerber is None:
        return []

    try:
        drill_layer = gerber.read(str(path))
    except Exception:
        return []

    try:
        # Normalize units to inch
        drill_layer.to_inch()
    except Exception:
        # If to_inch is unavailable, we assume it is already inch
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

    # Fallback: inspect hits and their tool diameters
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
            # Older formats: tool, (x, y)
            try:
                tool, _pos = hit
                d = getattr(tool, "diameter", None)
                if d is not None:
                    diameters_inch.append(float(d))
            except Exception:
                continue

    return [d_in * _INCH_TO_MM for d_in in diameters_inch]
