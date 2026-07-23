from __future__ import annotations

import math
from collections import defaultdict
from typing import List, Optional

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.gerber_backend import gerber_traces_mm
from ..results import CheckResult, MetricResult, Violation, ViolationLocation
from .impl_min_annular_ring import _point_in_polygon
from .impl_min_trace_spacing import (
    Segment,
    _conductor_groups,
    _segment_segment_distance_mm,
)
from .impl_min_trace_width import _MIN_MEANINGFUL_TRACE_MM


# --------------------------------------------------------------------------
# Geometry helpers (self-contained; mirror impl_solder_mask_expansion /
# impl_min_annular_ring edge-distance math).
# --------------------------------------------------------------------------
def _resolve_limit(check_def, key: str, default):
    """Resolve a threshold, preferring the pre-normalized ``check_def.limits``
    block; fall back to this check's ``metric.target``/``metric.limits`` (with
    um->mm scaling) when that plumbing is absent, so JSON thresholds are honored
    either way."""
    lim = getattr(check_def, "limits", None) or {}
    v = lim.get(key)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)

    metric = getattr(check_def, "metric", None) or {}
    units = str(metric.get("units", "mm")).lower()
    scale = 0.001 if units in ("um", "µm", "micron", "microns") else 1.0
    mapping = {
        "recommended_min": ("target", "min"),
        "recommended_max": ("target", "max"),
        "absolute_min": ("limits", "min"),
        "absolute_max": ("limits", "max"),
    }
    node_key, sub = mapping.get(key, (None, None))
    if node_key is not None:
        node = metric.get(node_key)
        if isinstance(node, dict):
            nv = node.get(sub)
            if isinstance(nv, (int, float)) and not isinstance(nv, bool):
                return float(nv) * scale
    return default


def _poly_area_mm2(poly) -> float:
    if hasattr(poly, "area_mm2"):
        return float(poly.area_mm2)
    if hasattr(poly, "area"):
        try:
            return float(poly.area())
        except TypeError:
            return float(poly.area)
    b = poly.bounds()
    return max(0.0, (b.max_x - b.min_x) * (b.max_y - b.min_y))


def _distance_point_to_segment(px, py, x1, y1, x2, y2) -> float:
    dx = x2 - x1
    dy = y2 - y1
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    if t < 0.0:
        return math.hypot(px - x1, py - y1)
    if t > 1.0:
        return math.hypot(px - x2, py - y2)
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _min_point_to_edges(x, y, vertices) -> float:
    n = len(vertices)
    if n < 2:
        return math.inf
    best = math.inf
    for i in range(n):
        j = (i + 1) % n
        d = _distance_point_to_segment(
            x, y, vertices[i].x, vertices[i].y, vertices[j].x, vertices[j].y
        )
        if d < best:
            best = d
    return best


def _min_distance_between_polygons(poly1, poly2) -> float:
    """Minimum edge-to-edge distance between two polygons (0 if they touch).

    Samples each polygon's vertices against the other's edges. Exact for the
    disjoint convex shapes (rectangular pads, trace strips) this check deals
    with; a safe lower-bound estimate for concave ones."""
    v1 = getattr(poly1, "vertices", None)
    v2 = getattr(poly2, "vertices", None)
    if not v1 or not v2:
        return math.inf
    best = math.inf
    for p in v1:
        d = _min_point_to_edges(p.x, p.y, v2)
        if d < best:
            best = d
    for p in v2:
        d = _min_point_to_edges(p.x, p.y, v1)
        if d < best:
            best = d
    return best


def _bbox_gap(b1, b2) -> float:
    """Lower-bound distance between two bounding boxes (0 if they overlap)."""
    dx = max(0.0, max(b1.min_x - b2.max_x, b2.min_x - b1.max_x))
    dy = max(0.0, max(b1.min_y - b2.max_y, b2.min_y - b1.max_y))
    return math.hypot(dx, dy)


class _Feature:
    __slots__ = ("side", "layer", "poly", "bounds", "cx", "cy")

    def __init__(self, side, layer, poly):
        self.side = side
        self.layer = layer
        self.poly = poly
        b = poly.bounds()
        self.bounds = b
        self.cx = 0.5 * (b.min_x + b.max_x)
        self.cy = 0.5 * (b.min_y + b.max_y)


def _opening_to_segment_clearance(opening_poly, seg: Segment) -> float:
    """Clearance in mm from a mask opening to a trace, negative if they overlap.

    Works on the trace's exact capsule (centerline + width) rather than on its
    tessellated outline polygon. Polygon-to-polygon edge distance cannot see
    containment -- a small opening sitting entirely inside a wide trace has a
    positive edge distance from it -- which is precisely the case that has to
    read as "overlapping" for the conductor test below to work.
    """
    # A trace endpoint or midpoint inside the opening is unambiguous overlap.
    mx = 0.5 * (seg.x1_mm + seg.x2_mm)
    my = 0.5 * (seg.y1_mm + seg.y2_mm)
    verts = opening_poly.vertices
    for px, py in ((seg.x1_mm, seg.y1_mm), (seg.x2_mm, seg.y2_mm), (mx, my)):
        if _point_in_polygon(px, py, verts):
            return -1.0

    best = math.inf
    n = len(verts)
    for i in range(n):
        j = (i + 1) % n
        edge = Segment("", verts[i].x, verts[i].y, verts[j].x, verts[j].y, 0.0)
        d, _mx, _my = _segment_segment_distance_mm(edge, seg)
        if d is not None and d < best:
            best = d
    if not math.isfinite(best):
        return math.inf
    # `best` is to the centerline; the copper reaches half a width further out.
    return best - 0.5 * seg.width_mm


def _seg_bounds(seg: Segment):
    half = 0.5 * seg.width_mm

    class _B:
        min_x = min(seg.x1_mm, seg.x2_mm) - half
        max_x = max(seg.x1_mm, seg.x2_mm) + half
        min_y = min(seg.y1_mm, seg.y2_mm) - half
        max_y = max(seg.y1_mm, seg.y2_mm) + half

    return _B


def _side_key(side) -> str:
    return str(side).lower() if side is not None else "unknown"


@register_check("mask_to_trace_clearance")
def run_mask_to_trace_clearance(ctx: CheckContext) -> CheckResult:
    """
    Minimum clearance from a solder-mask OPENING edge to a neighboring copper
    TRACE it does not belong to.

    This measures mask encroachment toward traces: if a mask opening (e.g. a pad
    opening) reaches too close to an adjacent routed trace, the trace copper gets
    exposed near the opening (tenting loss / bridging risk). It is NOT the same
    quantity as ``solder_mask_expansion`` (opening size vs its own pad); a
    previous version of this file wrongly duplicated that expansion computation.

    Method:
      - openings   = solder-mask-layer polygons (per side)
      - traces     = drawn copper segments (exact centerline + width), grouped
                     into physically connected conductors.
      - An opening BELONGS to every conductor it overlaps. Nothing in those
        conductors is a "neighbor": it is the pad the opening is for, and the
        routing attached to that pad, all on one net.
      - For each opening, measure the minimum clearance to segments of every
        OTHER conductor. Report the global minimum.

    This replaces a ``belongs_epsilon_mm`` distance threshold (1e-4 mm) that
    tried to skip "the opening's own trace" by proximity alone. It could only
    ever exclude the copper directly under the opening, not the rest of that
    pad's net, so the trace leaving the pad still counted as its own neighbor --
    and the reported minimum simply tracked the epsilon (0.00029 mm against a
    0.0001 mm threshold) instead of measuring the board (#14).

    Data-gap honesty: if there is no mask layer, no drawn copper, or every
    opening only touches its own conductor (so no neighbor clearance is
    measurable), we return not_applicable rather than silently reporting a
    bogus number. Mask polarity is assumed to be "openings" geometry (this
    engine cannot invert coverage geometry; see solder_mask_expansion).
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", "mm")

    recommended_min = _resolve_limit(ctx.check_def, "recommended_min", 0.05)  # mm
    absolute_min = _resolve_limit(ctx.check_def, "absolute_min", 0.025)       # mm

    raw_cfg = getattr(ctx.check_def, "raw", None) or {}
    mask_min_area_mm2 = float(raw_cfg.get("mask_min_area_mm2", 0.02))

    geom = ctx.geometry

    openings_by_side: dict[str, List[_Feature]] = defaultdict(list)
    traces_by_side: dict[str, List[_Feature]] = defaultdict(list)

    for layer in getattr(geom, "layers", []):
        layer_type = getattr(layer, "layer_type", getattr(layer, "type", None))
        side = getattr(layer, "side", None)
        logical = getattr(layer, "logical_layer", getattr(layer, "name", None))

        if layer_type == "mask":
            for poly in getattr(layer, "polygons", []):
                if _poly_area_mm2(poly) < mask_min_area_mm2:
                    continue
                openings_by_side[_side_key(side)].append(_Feature(side, logical, poly))

    # Copper comes from the drawn segments, not the tessellated outlines: the
    # exact centerline + width lets us test overlap (and hence conductor
    # membership) without a polygon boolean.
    segments_by_side: dict[str, List[Segment]] = defaultdict(list)
    for info in ctx.ingest.files:
        if info.layer_type != "copper":
            continue
        for t in gerber_traces_mm(info.path):
            if t.width_mm < _MIN_MEANINGFUL_TRACE_MM:
                continue  # region/pour boundary draw, not a real trace
            segments_by_side[_side_key(info.side)].append(Segment(
                layer_name=info.logical_layer,
                x1_mm=t.x1_mm, y1_mm=t.y1_mm,
                x2_mm=t.x2_mm, y2_mm=t.y2_mm,
                width_mm=t.width_mm,
            ))

    have_openings = any(openings_by_side.values())
    have_traces = any(segments_by_side.values())

    def _not_applicable(reason: str) -> CheckResult:
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            severity="info",  # overridden by finalize()
            status="not_applicable",
            score=100.0,
            metric=MetricResult.geometry_mm(
                measured_mm=None,
                target_mm=recommended_min,
                limit_low_mm=absolute_min,
            ),
            violations=[Violation(severity="info", message=reason, location=None)],
        ).finalize()

    if not have_openings:
        return _not_applicable(
            "No solder-mask opening geometry available; mask-to-trace clearance "
            "not applicable."
        )
    if not have_traces:
        return _not_applicable(
            "No trace-like copper detected; mask-to-trace clearance not applicable."
        )

    min_clearance = math.inf
    worst_loc: Optional[ViolationLocation] = None
    measured_any = False

    for side_key, openings in openings_by_side.items():
        segs = segments_by_side.get(side_key, [])
        if not segs:
            continue

        groups = _conductor_groups(segs)
        group_of = [groups.find(i) for i in range(len(segs))]
        seg_bounds = [_seg_bounds(sg) for sg in segs]

        for opening in openings:
            # Pass 1: which conductors does this opening sit on? Those are its
            # own pad and that pad's routing -- not neighbors. Overlap requires
            # the bounding boxes to overlap, so that is an exact cheap filter.
            owning: set = set()
            for i, sg in enumerate(segs):
                if _bbox_gap(opening.bounds, seg_bounds[i]) > 0.0:
                    continue
                if _opening_to_segment_clearance(opening.poly, sg) <= 0.0:
                    owning.add(group_of[i])

            # Pass 2: nearest copper belonging to any OTHER conductor.
            #
            # The bbox gap is a valid lower bound on the true clearance (every
            # point of a shape lies inside its bbox), so skipping pairs whose
            # bbox gap already exceeds the best clearance found is exact -- it
            # can never discard the true minimum. A fixed search radius is not:
            # it silently drops boards whose nearest neighbouring copper is
            # farther than the radius, reporting "not applicable" for a board
            # that has a perfectly good, if generous, clearance.
            for i, sg in enumerate(segs):
                if group_of[i] in owning:
                    continue
                if _bbox_gap(opening.bounds, seg_bounds[i]) >= min_clearance:
                    continue
                clearance = _opening_to_segment_clearance(opening.poly, sg)
                if clearance <= 0.0:
                    # Unreachable in practice: overlapping copper would have put
                    # this conductor in `owning` above. Guard anyway so a
                    # degenerate shape cannot report a negative clearance.
                    continue
                measured_any = True
                if clearance < min_clearance:
                    min_clearance = clearance
                    worst_loc = ViolationLocation(
                        layer=opening.layer,
                        x_mm=opening.cx,
                        y_mm=opening.cy,
                        notes=(
                            f"Mask opening edge to nearest neighboring trace on "
                            f"copper layer {sg.layer_name}."
                        ),
                    )

    if not measured_any or not math.isfinite(min_clearance):
        return _not_applicable(
            "Mask openings only coincide with their own traces/pads; no "
            "neighboring-trace clearance is measurable (mask-to-trace clearance "
            "not applicable for this artwork)."
        )

    measured = float(min_clearance)

    if measured < absolute_min:
        status = "fail"
        score = 0.0
        sev = "error"
    elif measured < recommended_min:
        status = "warning"
        sev = "warning"
        span = max(1e-6, recommended_min - absolute_min)
        frac = max(0.0, min(1.0, (measured - absolute_min) / span))
        score = max(0.0, min(100.0, 60.0 + 40.0 * frac))
    else:
        status = "pass"
        sev = "info"
        score = 100.0

    violations: List[Violation] = []
    if status != "pass":
        msg = (
            f"Minimum solder-mask-opening to neighboring-trace clearance is "
            f"{measured:.3f} mm (recommended >= {recommended_min:.3f} mm, absolute "
            f">= {absolute_min:.3f} mm). Mask is encroaching toward an adjacent "
            f"trace, risking exposed copper / bridging."
        )
        violations.append(Violation(severity=sev, message=msg, location=worst_loc))
    else:
        violations.append(
            Violation(
                severity="info",
                message=(
                    f"Minimum mask-opening to neighboring-trace clearance is "
                    f"{measured:.3f} mm (>= recommended {recommended_min:.3f} mm)."
                ),
                location=None,
            )
        )

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        severity="info",  # overridden by finalize()
        status=status,
        score=score,
        metric=MetricResult.geometry_mm(
            measured_mm=measured,
            target_mm=recommended_min,
            limit_low_mm=absolute_min,
        ),
        violations=violations,
    ).finalize()
