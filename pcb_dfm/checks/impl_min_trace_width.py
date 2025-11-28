from __future__ import annotations

from typing import List, Optional

from ..results import CheckResult, Violation, ViolationLocation
from ..engine.context import CheckContext
from ..engine.check_runner import register_check
from ..ingest import GerberFileInfo
from ..geometry import queries

# Use pcb-tools if available
try:
    import gerber
    from gerber.primitives import Line
except Exception:
    gerber = None
    Line = None  # type: ignore

_INCH_TO_MM = 25.4


@register_check("min_trace_width")
def run_min_trace_width(ctx: CheckContext) -> CheckResult:
    """
    Minimum trace width check.

    Instead of approximating from polygon bounding boxes (which is noisy and
    heavily affected by polygonization artifacts), we re-parse the original
    copper Gerber files and inspect Line primitives:

        - layer = gerber.read(path)
        - layer.to_inch()
        - for each Line primitive, take its width (or aperture width/diameter)

    Internal geometry and metrics are reported in mm.
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

    if gerber is None or Line is None or not copper_files:
        viol = Violation(
            severity="warning",
            message="Cannot compute minimum trace width (missing Gerber parser or no copper files).",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            severity=ctx.check_def.severity,
            status="warning",
            score=50.0,
            metric={
                "kind": "geometry",
                "units": units,
                "measured_value": None,
                "target": recommended_min,
                "limit_low": absolute_min,
                "limit_high": None,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    min_width_mm: Optional[float] = None
    worst_location: Optional[ViolationLocation] = None

    for info in copper_files:
        layer_name = info.logical_layer

        try:
            g_layer = gerber.read(str(info.path))
        except Exception:
            continue

        # Normalize to inch, then convert to mm
        try:
            g_layer.to_inch()
        except Exception:
            # assume already inch if to_inch not available
            pass

        for prim in getattr(g_layer, "primitives", []):
            if not isinstance(prim, Line):
                continue

            width_in = _get_line_width_inch(prim)
            if width_in is None:
                continue

            width_mm = width_in * _INCH_TO_MM

            # Compute a representative location: midpoint of the segment
            try:
                x1_in, y1_in = prim.start
                x2_in, y2_in = prim.end
                mx_mm = (x1_in + x2_in) * 0.5 * _INCH_TO_MM
                my_mm = (y1_in + y2_in) * 0.5 * _INCH_TO_MM
            except Exception:
                mx_mm = my_mm = None

            if min_width_mm is None or width_mm < min_width_mm:
                min_width_mm = width_mm
                if mx_mm is not None and my_mm is not None:
                    worst_location = ViolationLocation(
                        layer=layer_name,
                        x_mm=mx_mm,
                        y_mm=my_mm,
                        notes="Narrowest trace segment found from Gerber line width.",
                    )
                else:
                    worst_location = None

    if min_width_mm is None:
        viol = Violation(
            severity="warning",
            message="No trace segments found to compute minimum trace width.",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            severity=ctx.check_def.severity,
            status="warning",
            score=50.0,
            metric={
                "kind": "geometry",
                "units": units,
                "measured_value": None,
                "target": recommended_min,
                "limit_low": absolute_min,
                "limit_high": None,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    # Decide status based on mm
    if min_width_mm < absolute_min:
        status = "fail"
        severity = "error"
    elif min_width_mm < recommended_min:
        status = "warning"
        severity = "warning"
    else:
        status = "pass"
        severity = ctx.check_def.severity or "error"

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
        msg = (
            f"Minimum trace width {min_width_mm:.3f} mm is below "
            f"recommended {recommended_min:.3f} mm (absolute minimum {absolute_min:.3f} mm)."
        )
        violations.append(
            Violation(
                severity=severity,
                message=msg,
                location=worst_location,
            )
        )

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        severity=ctx.check_def.severity,
        status=status,
        score=score,
        metric={
            "kind": "geometry",
            "units": units,
            "measured_value": float(min_width_mm),
            "target": recommended_min,
            "limit_low": absolute_min,
            "limit_high": None,
            "margin_to_limit": margin_to_limit,
        },
        violations=violations,
    )


def _get_line_width_inch(prim) -> Optional[float]:
    """
    Try to extract a width in inches from a pcb-tools Line primitive.
    """
    width = getattr(prim, "width", None)
    if width is not None:
        try:
            return float(width)
        except Exception:
            pass

    ap = getattr(prim, "aperture", None)
    if ap is not None:
        for attr in ("width", "diameter", "size"):
            val = getattr(ap, attr, None)
            if val is not None:
                try:
                    return float(val)
                except Exception:
                    continue

    # Fallback to something tiny if we really cannot get it
    return None
