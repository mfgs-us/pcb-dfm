from __future__ import annotations

from pathlib import Path
from typing import List

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.gerber_backend import excellon_tool_diameters_mm
from ..ingest import GerberFileInfo
from ..results import CheckResult, MetricResult, Violation

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
                units="mm",  # Pydantic requires geometry metrics to use mm
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
                units="mm",  # Pydantic requires geometry metrics to use mm
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
                severity=ctx.check_def.severity,
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
            units="mm",  # Pydantic requires geometry metrics to use mm
            measured_value=min_d_mm,
            target=recommended_min,
            limit_low=absolute_min,
            limit_high=None,
            margin_to_limit=min_d_mm - absolute_min,
        ),
        violations=violations,
    ).finalize()


def _extract_tool_diameters_mm(path: Path) -> List[float]:
    """Drill tool diameters in mm, via the gerbonara parse backend (#3)."""
    return excellon_tool_diameters_mm(path)
