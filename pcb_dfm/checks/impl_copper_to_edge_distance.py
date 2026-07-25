# pcb_dfm/checks/impl_copper_to_edge_distance.py

from __future__ import annotations

import math
from typing import List, Optional

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry import queries
from ..geometry.gerber_backend import outline_contours_mm
from ..geometry.polygon_index import PolygonIndex
from ..geometry.primitives import Bounds, Point2D, Polygon
from ..results import CheckResult, MetricResult, Violation, ViolationLocation
from .impl_min_annular_ring import _min_distance_to_polygon_edges, _point_in_polygon
from .impl_solder_mask_expansion import _distance_point_to_segment

MAX_REPORTED_VIOLATIONS = 100


def _min_dist_polygon_to_segments(verts, segments) -> float:
    """Exact minimum distance from a polygon (``verts``) to a set of line
    segments, each ``(x1, y1, x2, y2)``.

    Mirrors ``_min_distance_between_polygons`` restricted to the given segments:
    every polygon vertex against each segment, plus each segment's endpoints
    against the polygon's edges. Used so a copper polygon is measured only
    against the *nearby* slice of the (possibly 1000+ vertex) board outline.
    """
    best = math.inf
    for v in verts:
        vx, vy = v.x, v.y
        for (x1, y1, x2, y2) in segments:
            dd = _distance_point_to_segment(vx, vy, x1, y1, x2, y2)
            if dd < best:
                best = dd
    for (x1, y1, x2, y2) in segments:
        d1 = _min_distance_to_polygon_edges(x1, y1, verts)
        if d1 < best:
            best = d1
        d2 = _min_distance_to_polygon_edges(x2, y2, verts)
        if d2 < best:
            best = d2
    return best


def _poly_area(poly: Polygon) -> float:
    v = poly.vertices
    s = 0.0
    n = len(v)
    for i in range(n):
        x1, y1 = v[i].x, v[i].y
        x2, y2 = v[(i + 1) % n].x, v[(i + 1) % n].y
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


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

    # Flatten the outline contours into individual segments and spatially index
    # them. Each copper polygon is then measured only against the nearby slice of
    # the boundary, not the whole (possibly 1000+ vertex, panelized) outline.
    # This preserves the exact distance for the near-edge copper that sets both
    # the minimum and the violations -- far copper cannot be either -- while
    # dropping the O(copper x total_outline_verts) cost that made a panelized
    # board take minutes.
    edge_segments: List[tuple[float, float, float, float]] = []
    seg_bounds: List[Bounds] = []
    for op in edge_polys:
        vs = op.vertices
        n = len(vs)
        for i in range(n):
            a = vs[i]
            b = vs[(i + 1) % n]
            edge_segments.append((a.x, a.y, b.x, b.y))
            seg_bounds.append(Bounds(min(a.x, b.x), min(a.y, b.y),
                                     max(a.x, b.x), max(a.y, b.y)))
    seg_index = PolygonIndex.from_bounds(list(enumerate(seg_bounds)))

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

            # Exact polygon-to-polygon distance is only needed for copper that
            # could either be the new global minimum or an offender (within the
            # recommended clearance). The bbox gap is a lower bound on the true
            # distance, so any copper whose gap already exceeds that threshold
            # cannot be either and is left at its (cheap) bbox-gap estimate. As
            # the running minimum shrinks the threshold tightens, so most of a
            # board's interior copper never pays for the exact O(verts^2) test --
            # it was the check's dominant cost. Capped at the original cutoff so
            # behaviour is never looser than before.
            exact_thr = min(cutoff, max(recommended_min, min_dist if min_dist is not None else cutoff))

            # Query only outline segments whose bbox is within exact_thr of this
            # copper's bbox; compute the exact distance to that local slice.
            q = Bounds(pb.min_x - exact_thr, pb.min_y - exact_thr,
                       pb.max_x + exact_thr, pb.max_y + exact_thr)
            near_ids = seg_index.query_bbox(q)
            if near_ids:
                d = _min_dist_polygon_to_segments(
                    poly.vertices, [edge_segments[i] for i in near_ids]
                )
            else:
                # No outline segment within exact_thr: this copper is farther
                # than the threshold from every edge, so it can be neither the
                # running minimum (exact_thr >= min_dist) nor an offender
                # (exact_thr >= recommended_min). Its exact value is irrelevant.
                d = exact_thr

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
