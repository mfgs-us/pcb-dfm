from __future__ import annotations

from typing import List, Optional

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.gerber_backend import GERBONARA_AVAILABLE, gerber_traces_mm
from ..ingest import GerberFileInfo
from ..results import CheckResult, MetricResult, Violation, ViolationLocation

# Line primitives thinner than this are region/pour boundary draws or artifacts,
# not fabricable traces (no fab makes sub-0.8-mil copper). Counting them makes a
# board with a copper pour report a spurious 0.000 mm minimum trace width.
_MIN_MEANINGFUL_TRACE_MM = 0.02

MAX_REPORTED_VIOLATIONS = 100


@register_check("min_trace_width")
def run_min_trace_width(ctx: CheckContext) -> CheckResult:
    """
    Minimum trace width check.

    Widths come from the drawn segments' apertures via the gerbonara parse
    backend (mm-native), which also covers arc-routed traces that the previous
    pcb-tools Line-only path could not see.
    """
    metric_cfg = ctx.check_def.metric or {}
    units_raw = metric_cfg.get("units", metric_cfg.get("unit", "mm"))
    # Normalize: we report mm for geometry metrics by default
    units = "mm" if units_raw in (None, "", "mm", "um") else units_raw

    limits = ctx.check_def.limits or {}
    # Interpreted as mm
    recommended_min = float(limits.get("recommended_min", 0.1))
    absolute_min = float(limits.get("absolute_min", 0.075))

    copper_files: List[GerberFileInfo] = [
        f for f in ctx.ingest.files if f.layer_type == "copper"
    ]

    if not GERBONARA_AVAILABLE or not copper_files:
        viol = Violation(
            severity="info",
            message="Cannot compute minimum trace width (missing Gerber parser or no copper files).",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="not_applicable",
            severity="info",  # Default value, will be overridden by finalize()
            score=100.0,
            metric=MetricResult.geometry_mm(
                measured_mm=None,
                target_mm=recommended_min,
                limit_low_mm=absolute_min,
            ),
            violations=[viol],
        ).finalize()

    min_width_mm: Optional[float] = None
    worst_location: Optional[ViolationLocation] = None

    # (width_mm, layer_name, mx_mm, my_mm)
    offenders: List[tuple[float, str, float, float]] = []

    for info in copper_files:
        layer_name = info.logical_layer
        for t in gerber_traces_mm(info.path):
            width_mm = t.width_mm
            if width_mm < _MIN_MEANINGFUL_TRACE_MM:
                continue  # region/pour boundary draw, not a real trace

            mx_mm = (t.x1_mm + t.x2_mm) * 0.5
            my_mm = (t.y1_mm + t.y2_mm) * 0.5

            if min_width_mm is None or width_mm < min_width_mm:
                min_width_mm = width_mm
                worst_location = ViolationLocation(
                    layer=layer_name,
                    x_mm=mx_mm,
                    y_mm=my_mm,
                    notes="Narrowest trace segment found from Gerber line width.",
                )

            if width_mm < recommended_min:
                offenders.append((width_mm, layer_name, mx_mm, my_mm))

    if min_width_mm is None:
        viol = Violation(
            severity="info",
            message="No trace segments found to compute minimum trace width.",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="not_applicable",
            severity="info",  # Default value, will be overridden by finalize()
            score=100.0,
            metric=MetricResult.geometry_mm(
                measured_mm=None,
                target_mm=recommended_min,
                limit_low_mm=absolute_min,
            ),
            violations=[viol],
        ).finalize()

    # Decide status only (severity handled by finalize)
    if min_width_mm < absolute_min:
        status = "fail"
    elif min_width_mm < recommended_min:
        status = "warning"
    else:
        status = "pass"

    # Score: linear between absolute_min and recommended_min
    if min_width_mm >= recommended_min:
        score = 100.0
    elif min_width_mm <= absolute_min:
        score = 0.0
    else:
        span = recommended_min - absolute_min
        score = max(0.0, min(100.0, 100.0 * (min_width_mm - absolute_min) / span))

    margin_to_limit = float(min_width_mm - absolute_min)

    violations: List[Violation] = []
    if status != "pass":
        # Sort offenders by width ascending (worst first)
        offenders_sorted = sorted(offenders, key=lambda t: t[0])
        if offenders_sorted:
            for width_mm, layer_name, mx_mm, my_mm in offenders_sorted[:MAX_REPORTED_VIOLATIONS]:
                msg = (
                    f"Trace segment width {width_mm:.3f} mm on layer {layer_name} is below "
                    f"recommended {recommended_min:.3f} mm (absolute minimum {absolute_min:.3f} mm)."
                )
                violations.append(
                    Violation(
                        severity=ctx.check_def.severity,
                        message=msg,
                        location=ViolationLocation(
                            layer=layer_name,
                            x_mm=mx_mm,
                            y_mm=my_mm,
                            notes="Trace segment below minimum width.",
                        ),
                    )
                )
        else:
            # Fallback: no per segment offenders captured, keep previous single summary
            msg = (
                f"Minimum trace width {min_width_mm:.3f} mm is below "
                f"recommended {recommended_min:.3f} mm (absolute minimum {absolute_min:.3f} mm)."
            )
            violations.append(
                Violation(
                    severity=ctx.check_def.severity,
                    message=msg,
                    location=worst_location,
                )
            )

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        severity="info",  # Default value, will be overridden by finalize()
        status=status,
        score=score,
        metric=MetricResult.geometry_mm(
            measured_mm=float(min_width_mm),
            target_mm=recommended_min,
            limit_low_mm=absolute_min,
        ),
        violations=violations,
    ).finalize()


