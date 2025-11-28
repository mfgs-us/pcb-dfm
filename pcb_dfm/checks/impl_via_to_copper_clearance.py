from __future__ import annotations

from typing import List, Optional
from dataclasses import dataclass

from ..geometry import queries
from ..geometry.primitives import Bounds, Point2D
from ..results import CheckResult, Violation, ViolationLocation
from ..engine.context import CheckContext
from ..engine.check_runner import register_check
from ..ingest import GerberFileInfo

try:
    import gerber
except Exception:
    gerber = None

_INCH_TO_MM = 25.4


@dataclass
class DrillHit:
    x_mm: float
    y_mm: float
    d_mm: float


@register_check("via_to_copper_clearance")
def run_via_to_copper_clearance(ctx: CheckContext) -> CheckResult:
    """
    Approximate via-to-copper clearance by:

      - Using all drill hits as via centers
      - For each via, computing min distance from via edge to any copper polygon
        whose bounds do NOT contain the via center (i.e. not its own pad).

    Metric:
      measured_value: min_via_to_copper_clearance_mm
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", metric_cfg.get("unit", "mm"))

    limits = ctx.check_def.limits or {}
    recommended_min = float(limits.get("recommended_min", 0.15))
    absolute_min = float(limits.get("absolute_min", 0.1))

    copper_layers = queries.get_copper_layers(ctx.geometry)

    drill_files: List[GerberFileInfo] = [
        f for f in ctx.ingest.files if f.layer_type == "drill"
    ]

    if gerber is None or not drill_files or not copper_layers:
        viol = Violation(
            severity="warning",
            message="Cannot compute via-to-copper clearance (missing drill parser, drills, or copper).",
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

    hits: List[DrillHit] = []
    for info in drill_files:
        hits.extend(_extract_drill_hits_mm(info.path))

    if not hits:
        viol = Violation(
            severity="warning",
            message="No drill hits found to compute via-to-copper clearance.",
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

    min_clear: Optional[float] = None
    worst_location: Optional[ViolationLocation] = None

    for hit in hits:
        cx = hit.x_mm
        cy = hit.y_mm
        r = hit.d_mm / 2.0

        for layer in copper_layers:
            for poly in layer.polygons:
                b: Bounds = poly.bounds()

                # Skip polygons whose bounds contain via center (assume it's the pad for this via)
                if (b.min_x <= cx <= b.max_x) and (b.min_y <= cy <= b.max_y):
                    continue

                # Compute distance from via center to polygon bounds
                dx = 0.0
                if cx < b.min_x:
                    dx = b.min_x - cx
                elif cx > b.max_x:
                    dx = cx - b.max_x

                dy = 0.0
                if cy < b.min_y:
                    dy = b.min_y - cy
                elif cy > b.max_y:
                    dy = cy - b.max_y

                if dx == 0.0 and dy == 0.0:
                    # Overlap or containment - treat as zero clearance
                    dist_center = 0.0
                else:
                    dist_center = (dx * dx + dy * dy) ** 0.5

                clearance = dist_center - r
                if clearance < 0:
                    clearance = 0.0

                if min_clear is None or clearance < min_clear:
                    min_clear = clearance
                    worst_location = ViolationLocation(
                        layer=layer.logical_layer,
                        x_mm=cx,
                        y_mm=cy,
                        notes="Via with minimum clearance to nearby copper (approx).",
                    )

    if min_clear is None:
        viol = Violation(
            severity="warning",
            message="Could not determine via-to-copper clearance.",
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

    # Decide status
    if min_clear < absolute_min:
        status = "fail"
        severity = "error"
    elif min_clear < recommended_min:
        status = "warning"
        severity = "warning"
    else:
        status = "pass"
        severity = ctx.check_def.severity or "error"

    # Score
    if min_clear >= recommended_min:
        score = 100.0
    elif min_clear <= absolute_min:
        score = 0.0
    else:
        span = recommended_min - absolute_min
        score = max(0.0, min(100.0, 100.0 * (min_clear - absolute_min) / span))

    margin_to_limit = float(min_clear - absolute_min)

    violations: List[Violation] = []
    if status != "pass":
        msg = (
            f"Minimum via-to-copper clearance {min_clear:.3f} mm is below "
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
            "measured_value": float(min_clear),
            "target": recommended_min,
            "limit_low": absolute_min,
            "limit_high": None,
            "margin_to_limit": margin_to_limit,
        },
        violations=violations,
    )


def _extract_drill_hits_mm(path) -> List[DrillHit]:
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

    hits_out: List[DrillHit] = []

    hits = getattr(drill_layer, "hits", None)
    if hits is None:
        return hits_out

    for hit in hits:
        x_in = y_in = d_in = None

        # New-style API
        try:
            if hasattr(hit, "x") and hasattr(hit, "y"):
                x_in = float(hit.x)
                y_in = float(hit.y)
            elif hasattr(hit, "position"):
                px, py = hit.position
                x_in = float(px)
                y_in = float(py)

            tool = getattr(hit, "tool", None)
            if tool is not None and hasattr(tool, "diameter"):
                d_in = float(tool.diameter)
        except Exception:
            pass

        # Old-style (tool, (x, y))
        if x_in is None or y_in is None or d_in is None:
            try:
                tool, (px, py) = hit
                x_in = float(px)
                y_in = float(py)
                d_in = float(getattr(tool, "diameter"))
            except Exception:
                continue

        hits_out.append(
            DrillHit(
                x_mm=x_in * _INCH_TO_MM,
                y_mm=y_in * _INCH_TO_MM,
                d_mm=d_in * _INCH_TO_MM,
            )
        )

    return hits_out
