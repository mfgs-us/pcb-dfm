from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..results import CheckResult, Violation, ViolationLocation
from ..engine.context import CheckContext
from ..engine.check_runner import register_check
from ..ingest import GerberFileInfo

# pcb-tools (same as min_trace_width impl)
try:
    import gerber
    from gerber.primitives import Line
except Exception:
    gerber = None
    Line = None  # type: ignore

_INCH_TO_MM = 25.4
MAX_REPORTED_VIOLATIONS = 100


@dataclass
class Segment:
    layer_name: str
    x1_mm: float
    y1_mm: float
    x2_mm: float
    y2_mm: float
    width_mm: float


@register_check("min_trace_spacing")
def run_min_trace_spacing(ctx: CheckContext) -> CheckResult:
    """
    Minimum trace spacing based on Gerber trace geometry.

    Approach:
      - Re-parse copper Gerber files.
      - Extract Line primitives (traces) with start/end in inch, convert to mm.
      - For each pair of segments on the same layer, compute:
            centerline distance between finite segments
            spacing = max(0, distance - 0.5*(w1 + w2))
      - Report the smallest positive spacing.

    This avoids polygonization / bbox artifacts and is much closer to what
    fabs mean by "trace spacing".
    """
    metric_cfg = ctx.check_def.metric or {}
    units_raw = metric_cfg.get("units", metric_cfg.get("unit", "mm"))
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
            message="Cannot compute minimum trace spacing (missing Gerber parser or no copper files).",
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

    # Collect segments per layer
    segments_by_layer: dict[str, List[Segment]] = {}

    for info in copper_files:
        try:
            g_layer = gerber.read(str(info.path))
        except Exception:
            continue

        try:
            g_layer.to_inch()
        except Exception:
            # assume already inch
            pass

        for prim in getattr(g_layer, "primitives", []):
            if not isinstance(prim, Line):
                continue

            width_in = _get_line_width_inch(prim)
            if width_in is None:
                continue

            try:
                (x1_in, y1_in) = prim.start
                (x2_in, y2_in) = prim.end
            except Exception:
                continue

            seg = Segment(
                layer_name=info.logical_layer,
                x1_mm=x1_in * _INCH_TO_MM,
                y1_mm=y1_in * _INCH_TO_MM,
                x2_mm=x2_in * _INCH_TO_MM,
                y2_mm=y2_in * _INCH_TO_MM,
                width_mm=width_in * _INCH_TO_MM,
            )
            segments_by_layer.setdefault(info.logical_layer, []).append(seg)

    min_spacing_mm: Optional[float] = None
    worst_location: Optional[ViolationLocation] = None

    # (spacing_mm, layer_name, mx_mm, my_mm)
    offenders: List[tuple[float, str, float, float]] = []

    for layer_name, segs in segments_by_layer.items():
        n = len(segs)
        if n < 2:
            continue

        for i in range(n):
            s1 = segs[i]
            for j in range(i + 1, n):
                s2 = segs[j]

                # Quick bbox reject
                if min_spacing_mm is not None:
                    if not _might_be_closer_than(s1, s2, min_spacing_mm):
                        continue

                dist_mm, mx_mm, my_mm = _segment_segment_distance_mm(s1, s2)
                if dist_mm is None:
                    continue

                # Copper-to-copper spacing = center distance - half widths
                spacing_mm = dist_mm - 0.5 * (s1.width_mm + s2.width_mm)
                if spacing_mm <= 0.0:
                    # overlapping or touching traces
                    continue

                if min_spacing_mm is None or spacing_mm < min_spacing_mm:
                    min_spacing_mm = spacing_mm
                    worst_location = ViolationLocation(
                        layer=layer_name,
                        x_mm=mx_mm,
                        y_mm=my_mm,
                        notes="Minimum spacing between copper traces (segment-based).",
                    )

                # Track all spacings that violate the recommended minimum
                if spacing_mm < recommended_min:
                    offenders.append((spacing_mm, layer_name, mx_mm, my_mm))

    if min_spacing_mm is None:
        viol = Violation(
            severity="warning",
            message="Not enough trace segments to compute minimum trace spacing.",
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
    if min_spacing_mm < absolute_min:
        status = "fail"
        severity = "error"
    elif min_spacing_mm < recommended_min:
        status = "warning"
        severity = "warning"
    else:
        status = "pass"
        severity = ctx.check_def.severity or "error"

    # Score
    if min_spacing_mm >= recommended_min:
        score = 100.0
    elif min_spacing_mm <= absolute_min:
        score = 0.0
    else:
        span = recommended_min - absolute_min
        score = max(0.0, min(100.0, 100.0 * (min_spacing_mm - absolute_min) / span))

    margin_to_limit = float(min_spacing_mm - absolute_min)

    violations: List[Violation] = []
    if status != "pass":
        offenders_sorted = sorted(offenders, key=lambda t: t[0])
        if offenders_sorted:
            for spacing_mm, layer_name, mx_mm, my_mm in offenders_sorted[:MAX_REPORTED_VIOLATIONS]:
                msg = (
                    f"Trace spacing {spacing_mm:.3f} mm on layer {layer_name} is below "
                    f"recommended {recommended_min:.3f} mm (absolute minimum {absolute_min:.3f} mm)."
                )
                violations.append(
                    Violation(
                        severity=severity,
                        message=msg,
                        location=ViolationLocation(
                            layer=layer_name,
                            x_mm=mx_mm,
                            y_mm=my_mm,
                            notes="Trace spacing below minimum.",
                        ),
                    )
                )
        else:
            msg = (
                f"Minimum trace spacing {min_spacing_mm:.3f} mm is below "
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
            "measured_value": float(min_spacing_mm),
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

    return None


def _might_be_closer_than(s1: Segment, s2: Segment, limit_mm: float) -> bool:
    """
    Cheap reject using bbox-like reasoning:
      if the centerlines are farther apart than limit_mm + half widths,
      they cannot improve the current minimum spacing.
    """
    # center points
    cx1 = 0.5 * (s1.x1_mm + s1.x2_mm)
    cy1 = 0.5 * (s1.y1_mm + s1.y2_mm)
    cx2 = 0.5 * (s2.x1_mm + s2.x2_mm)
    cy2 = 0.5 * (s2.y1_mm + s2.y2_mm)

    dx = cx2 - cx1
    dy = cy2 - cy1
    dist_center = (dx * dx + dy * dy) ** 0.5
    # Even if they touched, spacing could not be smaller than:
    # dist_center - half widths
    min_possible_spacing = dist_center - 0.5 * (s1.width_mm + s2.width_mm)
    return min_possible_spacing <= limit_mm


def _segment_segment_distance_mm(s1: Segment, s2: Segment) -> Tuple[Optional[float], float, float]:
    """
    Minimum distance between two finite line segments in mm, plus the midpoint
    of the closest points (for reporting).

    Returns:
      (distance_mm, mid_x_mm, mid_y_mm)
      If we cannot compute, returns (None, 0, 0).
    """
    p1 = (s1.x1_mm, s1.y1_mm)
    p2 = (s1.x2_mm, s1.y2_mm)
    q1 = (s2.x1_mm, s2.y1_mm)
    q2 = (s2.x2_mm, s2.y2_mm)

    # Use a parametric segment-segment distance (standard algorithm)
    d, cp1, cp2 = _closest_points_on_segments(p1, p2, q1, q2)
    if d is None:
        return None, 0.0, 0.0

    mx = 0.5 * (cp1[0] + cp2[0])
    my = 0.5 * (cp1[1] + cp2[1])
    return d, mx, my


def _closest_points_on_segments(
    p1: Tuple[float, float],
    p2: Tuple[float, float],
    q1: Tuple[float, float],
    q2: Tuple[float, float],
) -> Tuple[Optional[float], Tuple[float, float], Tuple[float, float]]:
    """
    Compute closest points on two segments p1-p2 and q1-q2 in 2D.

    Returns (distance, closest_point_on_p, closest_point_on_q).
    If degenerate, returns (None, (0,0), (0,0)).
    """
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = q1
    x4, y4 = q2

    # Segment directions
    ux = x2 - x1
    uy = y2 - y1
    vx = x4 - x3
    vy = y4 - y3
    wx = x1 - x3
    wy = y1 - y3

    a = ux * ux + uy * uy  # |u|^2
    b = ux * vx + uy * vy
    c = vx * vx + vy * vy  # |v|^2
    d = ux * wx + uy * wy
    e = vx * wx + vy * wy

    denom = a * c - b * b
    s, t = 0.0, 0.0

    if a <= 1e-12 and c <= 1e-12:
        # both segments degenerate
        return None, (0.0, 0.0), (0.0, 0.0)

    if denom != 0.0:
        s = (b * e - c * d) / denom
        t = (a * e - b * d) / denom

        # clamp to [0,1]
        s = max(0.0, min(1.0, s))
        t = max(0.0, min(1.0, t))
    else:
        # parallel or nearly so: fall back to endpoint-based search
        # by checking distances between endpoints and opposite segments.
        candidates = []

        def point_seg_dist(px, py, ax, ay, bx, by):
            vx = bx - ax
            vy = by - ay
            if vx * vx + vy * vy < 1e-12:
                dx = px - ax
                dy = py - ay
                return (dx * dx + dy * dy) ** 0.5, ax, ay
            t0 = ((px - ax) * vx + (py - ay) * vy) / (vx * vx + vy * vy)
            t0 = max(0.0, min(1.0, t0))
            cx = ax + t0 * vx
            cy = ay + t0 * vy
            dx = px - cx
            dy = py - cy
            return (dx * dx + dy * dy) ** 0.5, cx, cy

        for (px, py) in [p1, p2]:
            d1, cx1, cy1 = point_seg_dist(px, py, x3, y3, x4, y4)
            candidates.append((d1, (px, py), (cx1, cy1)))
        for (qx, qy) in [q1, q2]:
            d2, cx2, cy2 = point_seg_dist(qx, qy, x1, y1, x2, y2)
            candidates.append((d2, (cx2, cy2), (qx, qy)))

        dmin, cp_p, cp_q = min(candidates, key=lambda t: t[0])
        return dmin, cp_p, cp_q

    # Closest points using clamped s, t
    cp1 = (x1 + s * ux, y1 + s * uy)
    cp2 = (x3 + t * vx, y3 + t * vy)
    dx = cp1[0] - cp2[0]
    dy = cp1[1] - cp2[1]
    dist = (dx * dx + dy * dy) ** 0.5
    return dist, cp1, cp2
