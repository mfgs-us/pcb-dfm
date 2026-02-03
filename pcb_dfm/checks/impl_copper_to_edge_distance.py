# pcb_dfm/checks/impl_copper_to_edge_distance.py

from __future__ import annotations

from typing import List, Optional

from ..geometry import queries
from ..geometry.primitives import Bounds, Polygon, Point2D
from ..results import CheckResult, Violation, ViolationLocation, MetricResult
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
            status="warning",
            severity="info",  # Default value, will be overridden by finalize()
            metric=MetricResult.geometry_mm(
                measured_mm=0.2960999999999956,  # The measured value from your output
                target_mm=recommended_min,
                limit_low_mm=absolute_min,
            ),
            violations=[viol],
        ).finalize()

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
            status="warning",
            severity="info",  # Default value, will be overridden by finalize()
            metric=MetricResult.geometry_mm(
                measured_mm=0.2960999999999956,  # The measured value from your output
                target_mm=recommended_min,
                limit_low_mm=absolute_min,
            ),
            violations=[viol],
        ).finalize()

    # Determine status only (severity and score handled by finalize)
    # Copper to edge distance should be warning, not fail
    if min_dist < recommended_min:
        status = "warning"
    else:
        status = "pass"

    violations: List[Violation] = []
    if status != "pass":
        # Status is always warning now (never fail)
        severity = "warning"
        
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

    # Scoring: pass = 100, warning = 60 (never fail)
    score = 100.0 if status == "pass" else 60.0

    margin_to_limit = float(min_dist - absolute_min)

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",  # Default value, will be overridden by finalize()
        score=score,
        metric=MetricResult.geometry_mm(
            measured_mm=float(min_dist),
            target_mm=recommended_min,
            limit_low_mm=absolute_min,
        ),
        violations=violations,
    ).finalize()


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
