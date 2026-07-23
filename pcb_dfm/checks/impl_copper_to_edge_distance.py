# pcb_dfm/checks/impl_copper_to_edge_distance.py

from __future__ import annotations

import math
from typing import List, Optional

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry import queries
from ..geometry.gerber_backend import outline_contours_mm
from ..geometry.primitives import Bounds, Point2D, Polygon
from ..results import CheckResult, MetricResult, Violation, ViolationLocation
from .impl_min_annular_ring import _point_in_polygon
from .impl_solder_mask_expansion import _min_distance_between_polygons

MAX_REPORTED_VIOLATIONS = 100


def _poly_area(poly: Polygon) -> float:
    v = poly.vertices
    s = 0.0
    n = len(v)
    for i in range(n):
        x1, y1 = v[i].x, v[i].y
        x2, y2 = v[(i + 1) % n].x, v[(i + 1) % n].y
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


def _bbox_gap(a: Bounds, b: Bounds) -> float:
    """Straight-line gap between two bounding boxes (0 if they overlap). A valid
    lower bound on the true polygon-to-polygon distance, used to prune."""
    dx = max(0.0, a.min_x - b.max_x, b.min_x - a.max_x)
    dy = max(0.0, a.min_y - b.max_y, b.min_y - a.max_y)
    return math.hypot(dx, dy)


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

    # The board edge is derived from CLOSED contours assembled out of the outline
    # layer's stroked segments (#18), not from the raw outline geometry. An
    # outline layer routinely also carries dimension lines, registration/plot
    # marks and text; those are open chains and are dropped, so a stray mark just
    # outside the board is not mistaken for the edge (which previously matched its
    # own stray copper twin and reported 0.000 mm). The largest-area contour is
    # the board boundary; smaller closed contours are internal cutouts and slots,
    # which are also real edges.
    ingest = getattr(ctx, "ingest", None)
    outline_files = [
        f for f in (getattr(ingest, "files", None) or [])
        if getattr(f, "layer_type", None) == "outline"
    ]
    board_contour: Optional[Polygon] = None
    edge_polys: List[Polygon] = []
    for f in outline_files:
        for verts in outline_contours_mm(f.path):
            if len(verts) < 3:
                continue
            poly = Polygon(vertices=[Point2D(x=x, y=y) for (x, y) in verts])
            edge_polys.append(poly)
            if board_contour is None:  # contours come largest-area first
                board_contour = poly

    # Fall back to the outline layer's own polygons when nothing chained into a
    # closed contour (an exotic/broken outline export, or a geometry-only context
    # with no source files). The largest such polygon is the board boundary, so
    # off-board copper is still excluded.
    if not edge_polys:
        edge_polys = [
            p for lyr in ctx.geometry.get_layers_by_type("outline")
            for p in lyr.polygons if len(p.vertices) >= 3
        ]
        if edge_polys:
            board_contour = max(edge_polys, key=lambda p: _poly_area(p))

    if board_bounds is None or not copper_layers or not edge_polys:
        message = "No board outline or copper geometry available to compute copper to edge distance."
        viol = Violation(
            severity="info",
            message=message,
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="not_applicable",
            severity="info",  # Default value, will be overridden by finalize()
            metric=MetricResult.geometry_mm(
                measured_mm=None,
                target_mm=recommended_min,
                limit_low_mm=absolute_min,
            ),
            violations=[viol],
        ).finalize()

    min_dist: Optional[float] = None
    worst_location: Optional[ViolationLocation] = None

    # (dist_mm, layer_name, x_mm, y_mm)
    offenders: List[tuple[float, str, float, float]] = []

    # TRUE outline-polygon geometry: clearance to internal cutouts, slots, and
    # non-rectangular / concave edges is measured exactly. Copper farther than
    # `cutoff` from an outline contour can't violate, so we prune it with a cheap
    # bbox-gap lower bound and keep the exact O(verts) distance for near-edge
    # copper only.
    cutoff = max(2.0, recommended_min * 5.0)

    for layer in copper_layers:
        for poly in layer.polygons:
            pb = poly.bounds()
            loc_x, loc_y = 0.5 * (pb.min_x + pb.max_x), 0.5 * (pb.min_y + pb.max_y)

            # Copper outside the board boundary is not board copper -- it is the
            # same plot/registration artwork that also appears on the outline
            # layer (#18). Measuring it (against its own outline twin) is what
            # produced the 0.000 mm false failure, so skip it.
            if board_contour is not None and not _point_in_polygon(
                loc_x, loc_y, board_contour.vertices
            ):
                continue

            d = math.inf
            for op in edge_polys:
                gap = _bbox_gap(pb, op.bounds())
                # exact distance when close; the bbox gap (a lower bound) is
                # a fine stand-in for far contours that can't be the minimum
                dd = _min_distance_between_polygons(poly, op) if gap <= cutoff else gap
                if dd < d:
                    d = dd

            if min_dist is None or d < min_dist:
                min_dist = d
                worst_location = ViolationLocation(
                    layer=layer.logical_layer,
                    x_mm=loc_x,
                    y_mm=loc_y,
                    notes="Closest copper to board edge",
                )

            # Track any copper feature that violates the recommended minimum
            if d < recommended_min:
                offenders.append((d, layer.logical_layer, loc_x, loc_y))

    # If somehow no polygons, nothing to measure
    if min_dist is None:
        message = "No copper geometry available to compute copper to edge distance."
        viol = Violation(
            severity="info",
            message=message,
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="not_applicable",
            severity="info",  # Default value, will be overridden by finalize()
            metric=MetricResult.geometry_mm(
                measured_mm=None,
                target_mm=recommended_min,
                limit_low_mm=absolute_min,
            ),
            violations=[viol],
        ).finalize()

    # Determine status only (severity handled by finalize)
    if min_dist < absolute_min:
        status = "fail"
    elif min_dist < recommended_min:
        status = "warning"
    else:
        status = "pass"

    violations: List[Violation] = []
    if status != "pass":
        # Hard clearance violations are errors; softer ones are warnings.
        severity = "error" if status == "fail" else "warning"

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

    # Scoring: pass = 100, warning = 60, fail = 0
    if status == "pass":
        score = 100.0
    elif status == "warning":
        score = 60.0
    else:
        score = 0.0

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
