from __future__ import annotations

import math
from collections import defaultdict
from typing import List, Optional

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, MetricResult, Violation, ViolationLocation


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
      - traces     = copper polygons classified as trace-like (elongated: aspect
                     ratio >= trace_min_aspect), i.e. Line-type routed copper
                     rather than pads.
      - For each opening, measure the minimum edge-to-edge distance to every
        same-side trace the opening does NOT belong to (a trace essentially
        coincident with / under the opening -- distance <= belongs_epsilon -- is
        the opening's own feature and is skipped).
      - Report the global minimum clearance.

    Data-gap honesty: if there is no mask layer, no trace-like copper, or every
    opening only overlaps its own trace (so no neighbor clearance is
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
    trace_min_aspect = float(raw_cfg.get("trace_min_aspect_ratio", 3.0))
    belongs_epsilon = float(raw_cfg.get("belongs_epsilon_mm", 1e-4))
    # Only search traces whose bbox is within this margin of an opening -- a
    # clearance larger than this is comfortably safe and not worth the O(n^2).
    search_margin = float(raw_cfg.get("search_margin_mm", max(2.0, recommended_min * 20.0)))

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

        elif layer_type == "copper":
            for poly in getattr(layer, "polygons", []):
                b = poly.bounds()
                w = max(0.0, b.max_x - b.min_x)
                h = max(0.0, b.max_y - b.min_y)
                if w <= 0.0 or h <= 0.0:
                    continue
                short_dim = min(w, h)
                long_dim = max(w, h)
                aspect = long_dim / short_dim if short_dim > 0.0 else 1.0
                # Trace-like = elongated copper (routed line), not a pad blob.
                if aspect >= trace_min_aspect:
                    traces_by_side[_side_key(side)].append(_Feature(side, logical, poly))

    have_openings = any(openings_by_side.values())
    have_traces = any(traces_by_side.values())

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
        traces = traces_by_side.get(side_key, [])
        if not traces:
            continue

        for opening in openings:
            for tr in traces:
                gap = _bbox_gap(opening.bounds, tr.bounds)
                # Prune traces that are already farther than the best clearance
                # found, or comfortably beyond the search margin.
                if gap > search_margin and gap >= min_clearance:
                    continue
                if gap >= min_clearance:
                    continue

                dist = _min_distance_between_polygons(opening.poly, tr.poly)

                # The trace the opening belongs to (coincident / under it) is
                # not a "neighbor"; skip it.
                if dist <= belongs_epsilon:
                    continue

                measured_any = True
                if dist < min_clearance:
                    min_clearance = dist
                    worst_loc = ViolationLocation(
                        layer=opening.layer,
                        x_mm=opening.cx,
                        y_mm=opening.cy,
                        notes=(
                            f"Mask opening edge to nearest neighboring trace on "
                            f"copper layer {tr.layer}."
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
