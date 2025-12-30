# pcb_dfm/checks/impl_copper_to_edge_distance.py

from __future__ import annotations

from typing import List, Optional

from ..geometry import queries
from ..geometry.primitives import Bounds, Polygon, Point2D
from ..results import CheckResult, Violation, ViolationLocation
from ..engine.context import CheckContext
from ..engine.check_runner import register_check

MAX_REPORTED_VIOLATIONS = 100


@register_check("copper_to_edge_distance")
def run_copper_to_edge_distance(ctx: CheckContext) -> CheckResult:
    """
    Compute minimum copper to board edge distance across all copper layers.

    Metric:
      - min_copper_to_edge_mm: smallest distance (mm) from any copper polygon
        to the nearest board outline edge.

    Status:
      - pass: min >= recommended_min
      - warning: absolute_min <= min < recommended_min
      - fail: min < absolute_min
    """
    board_bounds = queries.get_board_bounds(ctx.geometry)
    copper_layers = queries.get_copper_layers(ctx.geometry)

    metric_cfg = ctx.check_def.metric or {}
    metric_id = metric_cfg.get("id", "min_copper_to_edge_mm")

    limits = ctx.check_def.limits or {}
    recommended_min = float(limits.get("recommended_min", 0.25))
    absolute_min = float(limits.get("absolute_min", 0.15))

    # No outline or no copper -> cannot measure, mark as warning
    if board_bounds is None or not copper_layers:
        message = "No board outline or copper layers available to compute copper to edge distance."
        viol = Violation(
            severity="warning",
            message=message,
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
                "units": metric_cfg.get("units", metric_cfg.get("unit", "mm")),
                "measured_value": None,
                "target": recommended_min,
                "limit_low": absolute_min,
                "limit_high": None,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    min_dist: Optional[float] = None
    worst_location: Optional[ViolationLocation] = None

    # (dist_mm, layer_name, x_mm, y_mm)
    offenders: List[tuple[float, str, float, float]] = []

    for layer in copper_layers:
        for poly in layer.polygons:
            pb = poly.bounds()
            d, loc = _distance_and_location_to_edge(pb, board_bounds)
            if min_dist is None or d < min_dist:
                min_dist = d
                worst_location = ViolationLocation(
                    layer=layer.logical_layer,
                    x_mm=loc.x,
                    y_mm=loc.y,
                    notes="Closest copper to board edge",
                )

            # Track any copper feature that violates the recommended minimum
            if d < recommended_min:
                offenders.append((d, layer.logical_layer, loc.x, loc.y))

    # If somehow no polygons, treat as warning but with metric None
    if min_dist is None:
        message = "No copper polygons available to compute copper to edge distance."
        viol = Violation(
            severity="warning",
            message=message,
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
                "units": metric_cfg.get("units", metric_cfg.get("unit", "mm")),
                "measured_value": None,
                "target": recommended_min,
                "limit_low": absolute_min,
                "limit_high": None,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    # Determine status, severity, and score
    if min_dist < absolute_min:
        status = "warning"
        severity = "error"
    elif min_dist < recommended_min:
        status = "warning"
        severity = "warning"
    else:
        status = "pass"
        severity = ctx.check_def.severity or "error"

    # Score: 0 at absolute_min or below, 100 at recommended_min or above, linear in between
    if min_dist >= recommended_min:
        score = 100.0
    elif min_dist <= absolute_min:
        score = 0.0
    else:
        span = recommended_min - absolute_min
        score = max(0.0, min(100.0, 100.0 * (min_dist - absolute_min) / span))

    violations: List[Violation] = []
    if status != "pass":
        offenders_sorted = sorted(offenders, key=lambda t: t[0])
        if offenders_sorted:
            for dist_mm, layer_name, x_mm, y_mm in offenders_sorted[:MAX_REPORTED_VIOLATIONS]:
                message = (
                    f"Copper feature is {dist_mm:.3f} mm from board edge on layer {layer_name}, "
                    f"below recommended {recommended_min:.3f} mm (absolute minimum {absolute_min:.3f} mm)."
                )
                violations.append(
                    Violation(
                        severity=severity,
                        message=message,
                        location=ViolationLocation(
                            layer=layer_name,
                            x_mm=x_mm,
                            y_mm=y_mm,
                            notes="Copper too close to board edge.",
                        ),
                    )
                )
        else:
            message = (
                f"Minimum copper to edge distance {min_dist:.3f} mm is below "
                f"recommended {recommended_min:.3f} mm (absolute minimum {absolute_min:.3f} mm)."
            )
            violations.append(
                Violation(
                    severity=severity,
                    message=message,
                    location=worst_location,
                )
            )

    margin_to_limit = float(min_dist - absolute_min)

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        severity=ctx.check_def.severity,
        status=status,
        score=score,
        metric={
            "kind": "geometry",
            "units": metric_cfg.get("units", metric_cfg.get("unit", "mm")),
            "measured_value": float(min_dist),
            "target": recommended_min,
            "limit_low": absolute_min,
            "limit_high": None,
            "margin_to_limit": margin_to_limit,
        },
        violations=violations,
    )


def _distance_and_location_to_edge(poly_bounds: Bounds, board_bounds: Bounds) -> tuple[float, Point2D]:
    """
    Compute the minimum distance from a polygon bounds to the board bounds,
    and return both the distance and an approximate location point.

    We consider distance to each of the four edges of the board and
    choose the smallest one.
    """
    d_left = poly_bounds.min_x - board_bounds.min_x
    d_right = board_bounds.max_x - poly_bounds.max_x
    d_bottom = poly_bounds.min_y - board_bounds.min_y
    d_top = board_bounds.max_y - poly_bounds.max_y

    distances = [
        ("left", d_left),
        ("right", d_right),
        ("bottom", d_bottom),
        ("top", d_top),
    ]
    edge, d_min = min(distances, key=lambda t: t[1])

    # Location: approximate by midpoint of the touching segment
    if edge == "left":
        x = poly_bounds.min_x
        y = (poly_bounds.min_y + poly_bounds.max_y) / 2.0
    elif edge == "right":
        x = poly_bounds.max_x
        y = (poly_bounds.min_y + poly_bounds.max_y) / 2.0
    elif edge == "bottom":
        x = (poly_bounds.min_x + poly_bounds.max_x) / 2.0
        y = poly_bounds.min_y
    else:  # top
        x = (poly_bounds.min_x + poly_bounds.max_x) / 2.0
        y = poly_bounds.max_y

    return d_min, Point2D(x=x, y=y)
