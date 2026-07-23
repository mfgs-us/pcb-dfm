from __future__ import annotations

import math
from collections import defaultdict
from typing import List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.polygon_index import PolygonIndex
from ..geometry.primitives import Bounds, Polygon
from ..results import CheckResult, MetricResult, Violation, ViolationLocation


def _resolve_limit(check_def, key: str, default):
    """Resolve a threshold (recommended_min/max, absolute_min/max), preferring
    the pre-normalized ``check_def.limits`` block (target -> recommended_*,
    limits -> absolute_*). If that plumbing is absent, fall back to deriving the
    value directly from this check's ``metric.target``/``metric.limits`` with
    um->mm scaling, so the JSON thresholds are honored either way."""
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

    # Calculate area from vertices if available
    if hasattr(poly, "vertices") and len(poly.vertices) >= 3:
        return _polygon_area_from_vertices(poly.vertices)

    # Fallback to bounding box
    b = poly.bounds()
    return max(0.0, (b.max_x - b.min_x) * (b.max_y - b.min_y))


def _polygon_area_from_vertices(vertices) -> float:
    """Calculate polygon area using shoelace formula."""
    if len(vertices) < 3:
        return 0.0

    area = 0.0
    n = len(vertices)
    for i in range(n):
        j = (i + 1) % n
        area += vertices[i].x * vertices[j].y
        area -= vertices[j].x * vertices[i].y

    return abs(area) / 2.0


def _get_board_outline_area(geom) -> float:
    """Get total board outline area for polarity detection."""
    total_area = 0.0

    for layer in getattr(geom, "layers", []):
        layer_type = getattr(layer, "layer_type", getattr(layer, "type", None))

        if layer_type == "outline":
            for poly in getattr(layer, "polygons", []):
                total_area += _poly_area_mm2(poly)

    return total_area


def _normalize_mask_polarity(mask_polygons: List[Polygon], board_area: float) -> Tuple[List[Polygon], str]:
    """
    3A) Normalize solder mask layer into "openings geometry".

    CAVEAT (known unresolved hard case): this function ALWAYS returns the input
    polygons tagged as "openings". Correctly distinguishing openings-geometry
    from coverage-geometry (and inverting the latter) requires boolean polygon
    operations against the board outline, which this engine does not yet have.
    The heuristics below are computed but deliberately do not change the outcome
    -- we bias toward "openings" (the common export convention) to avoid
    inverting geometry we cannot reliably invert, which would produce false
    fails. Callers must treat mask polarity as an assumption, not a fact.

    Returns:
        - List of normalized opening polygons (always the input, unchanged)
        - String indicating polarity: always "openings" (see caveat)
    """
    if board_area <= 0:
        # No board outline available, assume openings (most common)
        return mask_polygons, "openings"

    # Calculate robust polarity detection metrics
    total_mask_area = sum(_poly_area_mm2(poly) for poly in mask_polygons)
    max_poly_area = max(_poly_area_mm2(poly) for poly in mask_polygons) if mask_polygons else 0.0
    n_polys = len(mask_polygons)

    # Robust heuristic:
    # - If there's one giant polygon roughly board-sized, it's coverage
    # - If total area is small relative to board, it's openings
    # - Default to openings (bias toward openings because we cannot invert without boolean ops)
    area_ratio = total_mask_area / board_area if board_area > 0 else 0.0
    max_poly_ratio = max_poly_area / board_area if board_area > 0 else 0.0

    if max_poly_ratio > 0.8:
        # One giant polygon roughly board-sized -> coverage
        # But we don't have robust inversion, so default to openings
        return mask_polygons, "openings"
    elif area_ratio < 0.6:
        # Total area is small -> openings
        return mask_polygons, "openings"
    else:
        # We do not have robust inversion here; default to openings to avoid false fails.
        return mask_polygons, "openings"


def _invert_mask_coverage(coverage_polygons: List[Polygon], board_area: float) -> List[Polygon]:
    """
    Convert coverage polygons to opening polygons.

    For now, this is a simplified implementation.
    In a full implementation, you'd need proper boolean operations.
    """
    # This is a placeholder - proper implementation would require
    # boolean geometry operations (outline - coverage = openings)
    #
    # For now, we'll return the original polygons but mark as inverted
    # The expansion calculation will need to handle this case
    return coverage_polygons


def _distance_point_to_segment(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    """Calculate minimum distance from point to line segment."""
    dx = x2 - x1
    dy = y2 - y1

    if abs(dx) < 1e-10 and abs(dy) < 1e-10:
        # Segment is a point
        return math.sqrt((px - x1)**2 + (py - y1)**2)

    # Parameter t determines closest point on infinite line
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)

    if t < 0:
        # Closest point is segment start
        return math.sqrt((px - x1)**2 + (py - y1)**2)
    elif t > 1:
        # Closest point is segment end
        return math.sqrt((px - x2)**2 + (py - y2)**2)
    else:
        # Closest point is interior to segment
        closest_x = x1 + t * dx
        closest_y = y1 + t * dy
        return math.sqrt((px - closest_x)**2 + (py - closest_y)**2)


def _min_distance_between_polygons(poly1, poly2) -> float:
    """Calculate minimum distance between two polygons."""
    if not hasattr(poly1, 'vertices') or not hasattr(poly2, 'vertices'):
        return float('inf')

    min_dist = float('inf')

    # Check distances from vertices of poly1 to edges of poly2
    for vertex in poly1.vertices:
        dist = _min_distance_to_polygon_edges(vertex.x, vertex.y, poly2.vertices)
        min_dist = min(min_dist, dist)

    # Check distances from vertices of poly2 to edges of poly1
    for vertex in poly2.vertices:
        dist = _min_distance_to_polygon_edges(vertex.x, vertex.y, poly1.vertices)
        min_dist = min(min_dist, dist)

    return min_dist


def _min_distance_to_polygon_edges(x: float, y: float, vertices) -> float:
    """Find minimum distance from point to any polygon edge."""
    if len(vertices) < 3:
        return float('inf')

    min_dist = float('inf')
    n = len(vertices)

    for i in range(n):
        j = (i + 1) % n
        x1, y1 = vertices[i].x, vertices[i].y
        x2, y2 = vertices[j].x, vertices[j].y

        dist = _distance_point_to_segment(x, y, x1, y1, x2, y2)
        min_dist = min(min_dist, dist)

    return min_dist


def _bbox_intersects(a, b) -> bool:
    """Fallback bbox intersection test."""
    ba = a.bounds()
    bb = b.bounds()
    return not (ba.max_x < bb.min_x or ba.min_x > bb.max_x or ba.max_y < bb.min_y or ba.min_y > bb.max_y)


def _intersects(a, b) -> bool:
    if hasattr(a, "intersects"):
        return bool(a.intersects(b))
    # fallback: use bbox intersection
    return _bbox_intersects(a, b)


def _bbox_contains(a, b) -> bool:
    """Fallback bbox containment test."""
    ba = a.bounds()
    bb = b.bounds()
    return (ba.min_x <= bb.min_x and ba.max_x >= bb.max_x and
            ba.min_y <= bb.min_y and ba.max_y >= bb.max_y)


def _contains(a, b) -> bool:
    if hasattr(a, "contains"):
        return bool(a.contains(b))
    # fallback: use bbox containment
    return _bbox_contains(a, b)


# A mask opening is treated as serving a pad only if the pad fills at least this
# fraction of the opening. Below it, the copper is a trace passing through, or
# the opening is shared over a cluster -- neither is a mask-on-pad defect.
_MASK_ON_PAD_MIN_COVERAGE = 0.7


def _opening_fill(pad, mask) -> float:
    """Fraction of the mask OPENING's bbox area that the pad fills.

    This is the test for 'does this opening serve this pad', and it must be
    opening-coverage, not pad-coverage. A pad fills its own opening whether the
    opening is generously expanded (~0.8) or undersized/mask-on-pad (~1.0). A
    trace merely passing through an opening meant for the pad at its end clips
    only part of it (~0.5), and a large opening shared over a cluster is filled
    only fractionally by any one pad (~0.2). Pad-coverage cannot tell a
    mask-on-pad defect (opening smaller than pad -> low pad-coverage) from a
    trace (also low pad-coverage), which is the whole point.
    """
    ox = max(0.0, min(pad.max_x, mask.max_x) - max(pad.min_x, mask.min_x))
    oy = max(0.0, min(pad.max_y, mask.max_y) - max(pad.min_y, mask.min_y))
    mask_area = (mask.max_x - mask.min_x) * (mask.max_y - mask.min_y)
    if mask_area <= 0.0:
        return 0.0
    return (ox * oy) / mask_area


@register_check("solder_mask_expansion")
def run_solder_mask_expansion(ctx: CheckContext) -> CheckResult:
    """
    Estimate solder mask expansion around pad like copper features.

    For each pad candidate on copper layers, find the smallest enclosing mask
    opening on the same side, then compute an approximate expansion:
        expansion_mm = 0.5 * min(mask_w - pad_w, mask_h - pad_h)

    All internal geometry in mm.
    Metric is reported in mm, even if JSON used um.
    """
    # Thresholds come pre-normalized to mm from the shared plumbing
    # (ctx.check_def.limits: target -> recommended_*, limits -> absolute_*).
    # For this check the JSON declares a target_range, so we get a lower bound
    # (min expansion; too small => mask-on-pad) AND an upper bound (max
    # expansion; too large => exposed copper / bridging risk).
    recommended_min = _resolve_limit(ctx.check_def, "recommended_min", 0.05)
    absolute_min = _resolve_limit(ctx.check_def, "absolute_min", 0.0)
    recommended_max = _resolve_limit(ctx.check_def, "recommended_max", None)
    absolute_max = _resolve_limit(ctx.check_def, "absolute_max", None)

    # Raw parameters
    raw_cfg = getattr(ctx.check_def, "raw", None) or {}
    pad_min_area_mm2 = float(raw_cfg.get("pad_min_area_mm2", 0.02))
    pad_max_area_mm2 = float(raw_cfg.get("pad_max_area_mm2", 4.0))
    # 4:1 caps what this bbox-based check treats as a pad. Beyond that, elongated
    # copper is far more likely a trace, a pour finger, or an edge-connector
    # tab than a component pad, and the pad-vs-opening bbox math cannot reason
    # about it (a trace's opening is for the pad at its end, not the trace). The
    # earlier 10:1 let 6:1-9:1 traces through, which drove mask-on-pad false
    # positives on every real board even after the through-opening guard (#19).
    pad_max_aspect_ratio = float(raw_cfg.get("pad_max_aspect_ratio", 4.0))
    mask_min_area_mm2 = float(raw_cfg.get("mask_min_area_mm2", 0.02))
    mask_search_inflate_mm = float(raw_cfg.get("mask_search_inflate_mm", 0.05))

    geom = ctx.geometry

    class _Pad:
        __slots__ = ("side", "layer", "poly", "min_x", "max_x", "min_y", "max_y", "cx", "cy")

        def __init__(self, side, layer, poly):
            self.side = side
            self.layer = layer
            self.poly = poly
            b = poly.bounds()
            self.min_x = b.min_x
            self.max_x = b.max_x
            self.min_y = b.min_y
            self.max_y = b.max_y
            self.cx = 0.5 * (b.min_x + b.max_x)
            self.cy = 0.5 * (b.min_y + b.max_y)

    class _MaskOpening:
        __slots__ = ("side", "layer", "poly", "min_x", "max_x", "min_y", "max_y", "area", "polarity")

        def __init__(self, side, layer, poly, polarity="openings"):
            self.side = side
            self.layer = layer
            self.poly = poly
            self.polarity = polarity  # "openings" or "coverage"
            b = poly.bounds()
            self.min_x = b.min_x
            self.max_x = b.max_x
            self.min_y = b.min_y
            self.max_y = b.max_y
            self.area = _poly_area_mm2(poly)

    pads: List[_Pad] = []
    raw_masks_by_side: dict[str, List[Tuple[Polygon, str, str]]] = defaultdict(list)  # side -> (poly, layer, logical)

    # First pass: collect copper pads and raw mask polygons by side
    for layer in getattr(geom, "layers", []):
        layer_type = getattr(layer, "layer_type", getattr(layer, "type", None))
        side = getattr(layer, "side", None)
        logical = getattr(layer, "logical_layer", getattr(layer, "name", None))

        if layer_type == "copper":
            for poly in getattr(layer, "polygons", []):
                area = _poly_area_mm2(poly)
                if area < pad_min_area_mm2 or area > pad_max_area_mm2:
                    continue
                b = poly.bounds()
                w = max(0.0, b.max_x - b.min_x)
                h = max(0.0, b.max_y - b.min_y)
                if w <= 0.0 or h <= 0.0:
                    continue
                short_dim = min(w, h)
                long_dim = max(w, h)
                aspect = long_dim / short_dim if short_dim > 0.0 else 1.0
                if aspect > pad_max_aspect_ratio:
                    continue
                pads.append(_Pad(side, logical, poly))

        elif layer_type == "mask":
            for poly in getattr(layer, "polygons", []):
                area = _poly_area_mm2(poly)
                if area < mask_min_area_mm2:
                    continue
                side_key = str(side).lower() if side is not None else "unknown"
                raw_masks_by_side[side_key].append((poly, layer, logical))

    # 3A) Normalize mask polarity for each side
    board_area = _get_board_outline_area(geom)
    masks: List[_MaskOpening] = []

    for side_key, mask_polys in raw_masks_by_side.items():
        if not mask_polys:
            continue

        # Extract polygons for this side
        side_polygons = [poly for poly, _, _ in mask_polys]
        layer_name = mask_polys[0][1]  # Use first layer for naming
        logical_name = mask_polys[0][2]

        # Normalize polarity
        normalized_polys, polarity = _normalize_mask_polarity(side_polygons, board_area)

        # Create mask opening objects
        for poly in normalized_polys:
            masks.append(_MaskOpening(side_key, logical_name, poly, polarity))

    if not pads:
        viol = Violation(
            severity="info",
            message="No pad like copper features detected to estimate solder mask expansion.",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="pass",
            severity="info",  # Default value, will be overridden by finalize()
            score=100.0,
            metric=MetricResult.geometry_mm(
                measured_mm=None,
                target_mm=recommended_min,
                limit_low_mm=absolute_min,
            ),
            violations=[viol],
        ).finalize()

    # Spatial index for mask openings to avoid pads*masks scans.
    #
    # Each mask is indexed by its bbox inflated by mask_search_inflate_mm, and a
    # pad's candidates are the masks touching the block of cells around the pad
    # centroid (ring = ceil(inflate/cell) + 1). Indexing the inflated bbox and
    # querying that cell block reproduces the previous hand-rolled grid exactly
    # (same cell size, same floor math, same first-seen candidate order), so the
    # downstream intersection / containment / area-rank logic is unchanged and
    # results are identical; the index only prunes masks too far to ever
    # intersect a pad.
    cell = max(0.5, mask_search_inflate_mm * 4.0)  # mm, tuned for typical mask clearances
    k = int(math.ceil(mask_search_inflate_mm / cell)) + 1
    mask_index_by_side: dict[str, PolygonIndex] = {}
    masks_by_side: dict[str, list[_MaskOpening]] = defaultdict(list)

    for m in masks:
        key = str(m.side).lower() if m.side is not None else "unknown"
        masks_by_side[key].append(m)

    infl = mask_search_inflate_mm
    for side_key, side_masks in masks_by_side.items():
        id_bounds = [
            (
                idx,
                Bounds(m.min_x - infl, m.min_y - infl, m.max_x + infl, m.max_y + infl),
            )
            for idx, m in enumerate(side_masks)
        ]
        mask_index_by_side[side_key] = PolygonIndex.from_bounds(
            id_bounds, cell_size=cell
        )

    # helper to fetch candidate masks for a pad
    def _mask_candidates_for_pad(pad: _Pad) -> list[_MaskOpening]:
        side_key = str(pad.side).lower() if pad.side is not None else "unknown"
        side_masks = masks_by_side.get(side_key, [])
        index = mask_index_by_side.get(side_key)
        if not index:
            return []
        ci, cj = index.cell_of(pad.cx, pad.cy)
        return [side_masks[midx] for midx in index.items_in_cell_block(ci, cj, ring=k)]

    min_expansion = math.inf
    min_loc: Optional[ViolationLocation] = None
    max_expansion = -math.inf
    max_loc: Optional[ViolationLocation] = None
    has_any_match = False

    for pad in pads:
        pad_w = pad.max_x - pad.min_x
        pad_h = pad.max_y - pad.min_y

        best_rank_for_pad = (2, math.inf)
        best_mask_for_pad: Optional[_MaskOpening] = None

        for m in _mask_candidates_for_pad(pad):
            if pad.side and m.side and str(pad.side).lower() != str(m.side).lower():
                continue

            if not _intersects(m.poly, pad.poly):
                continue

            contains = _contains(m.poly, pad.poly)
            rank = (0 if contains else 1, m.area)

            if rank < best_rank_for_pad:
                best_rank_for_pad = rank
                best_mask_for_pad = m

        if best_mask_for_pad is None:
            continue

        has_any_match = True

        m = best_mask_for_pad

        # 3B) Compute expansion using true distance measurement with proper sign logic
        try:
            min_distance = _min_distance_between_polygons(pad.poly, m.poly)

            # Use containment-based sign logic instead of distance magnitude
            if _contains(m.poly, pad.poly):
                # Mask fully contains pad, expansion is positive
                expansion = min_distance
                notes = "True distance-based expansion measurement (pad contained in mask)"
            elif _opening_fill(pad, m) < _MASK_ON_PAD_MIN_COVERAGE:
                # Copper that only passes THROUGH the opening is not a pad the
                # mask is encroaching on -- it is the trace leaving the pad, and
                # every pad on every board has one. The signed bbox formula below
                # reads such a trace as a huge mask-on-pad defect, because the
                # trace is longer than the opening it exits: on eagle_gyw a
                # 1.016 x 3.556 mm trace crossing a 1.999 mm round opening gave
                # 0.5 * min(+0.983, -1.557) = -0.78 mm. That is why this check
                # failed 100% of real boards (#19).
                continue
            else:
                # Mask does not fully contain the pad, but the pad is mostly
                # inside it -> the opening really is undersized or offset, and
                # part of the pad sits under mask (mask-on-pad). Use a SIGNED
                # bbox approximation: a mask bbox smaller than the pad yields a
                # NEGATIVE expansion, which is a real defect and must be allowed
                # to trip the lower fail branch (do NOT clamp to zero, which is
                # what previously made a fail unreachable).
                mask_w = m.max_x - m.min_x
                mask_h = m.max_y - m.min_y
                dx = mask_w - pad_w
                dy = mask_h - pad_h
                expansion = 0.5 * min(dx, dy)
                notes = "Bbox approximation (pad not fully contained; possible mask-on-pad)"
        except Exception:
            # Fallback to (signed) bbox approximation if distance calc fails
            mask_w = m.max_x - m.min_x
            mask_h = m.max_y - m.min_y
            dx = mask_w - pad_w
            dy = mask_h - pad_h
            expansion = 0.5 * min(dx, dy)
            notes = "Fallback bbox approximation"

        if expansion < min_expansion:
            min_expansion = expansion
            min_loc = ViolationLocation(
                layer=m.layer,
                x_mm=pad.cx,
                y_mm=pad.cy,
                notes=notes,
            )
        if expansion > max_expansion:
            max_expansion = expansion
            max_loc = ViolationLocation(
                layer=m.layer,
                x_mm=pad.cx,
                y_mm=pad.cy,
                notes="Largest mask expansion (over-expansion / exposed-copper candidate)",
            )

    if not has_any_match or not math.isfinite(min_expansion):
        viol = Violation(
            severity="info",
            message="Could not match solder mask openings to pads to estimate expansion; mask polarity normalization may have failed.",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="warning",
            severity="info",  # Default value, will be overridden by finalize()
            score=50.0,
            metric=MetricResult.geometry_mm(
                measured_mm=None,
                target_mm=recommended_min,
                limit_low_mm=absolute_min,
            ),
            violations=[viol],
        ).finalize()

    measured = float(min_expansion)
    max_measured = float(max_expansion) if math.isfinite(max_expansion) else measured

    # Evaluate BOTH failure directions:
    #  - under-expansion (opening too small / mask-on-pad): measured < min bounds
    #  - over-expansion (opening far larger than pad -> exposed copper / bridging):
    #    max_measured > the max bounds (only if the JSON declared a max).
    # ADVISORY ONLY -- never a hard fail.
    #
    # This estimate is bbox-based and has no netlist or footprint data, so it
    # cannot reliably tell a component pad from an elongated feature, nor which
    # opening serves which pad. Across three real boards from three CAD tools it
    # was the residual failures after the trace-through-opening fix -- an opening
    # shared over a cluster read as gross over-expansion, an elongated pad partly
    # under mask read as mask-on-pad, and a 13 um shortfall (well inside fab
    # registration tolerance) read as a defect (#19). None of those is reliable
    # enough to fail a board on. A real mask-on-pad or exposed-copper problem
    # still surfaces as a warning for review. Registration-scale differences
    # (|expansion| below one fab registration tolerance) are treated as "mask
    # aligned to pad", not a shortfall.
    registration_tol = float(raw_cfg.get("registration_tolerance_mm", 0.05))

    # Never hard-fails (see note above); under_fail/over_fail retained as False
    # so the violation-message code below reads uniformly.
    under_fail = False
    over_fail = False
    under_warn = measured < recommended_min and measured < -registration_tol
    over_warn = (recommended_max is not None) and (max_measured > recommended_max)

    if under_warn or over_warn:
        status = "warning"
        span = max(1e-6, recommended_min - absolute_min)
        frac = (measured - absolute_min) / span
        score = max(0.0, min(100.0, 60.0 + 40.0 * max(0.0, min(1.0, frac))))
    else:
        status = "pass"
        score = 100.0

    margin_to_limit = float(measured - absolute_min)

    violations: List[Violation] = []

    # Under-expansion violation
    if under_fail or under_warn:
        sev = "error" if under_fail else "warning"
        if measured < 0.0:
            umsg = (
                f"Solder mask opening is smaller than the pad by {-measured:.3f} mm "
                f"(mask-on-pad): the opening must be at least {absolute_min:.3f} mm "
                f"larger than the pad (recommended >= {recommended_min:.3f} mm)."
            )
        else:
            umsg = (
                f"Minimum solder mask expansion is {measured:.3f} mm "
                f"(recommended >= {recommended_min:.3f} mm, absolute >= {absolute_min:.3f} mm)."
            )
        violations.append(Violation(severity=sev, message=umsg, location=min_loc))

    # Over-expansion violation
    if over_fail or over_warn:
        sev = "error" if over_fail else "warning"
        rec_txt = f"{recommended_max:.3f}" if recommended_max is not None else "n/a"
        abs_txt = f"{absolute_max:.3f}" if absolute_max is not None else "n/a"
        omsg = (
            f"Maximum solder mask expansion is {max_measured:.3f} mm, larger than the "
            f"pad opening should be (recommended <= {rec_txt} mm, absolute <= {abs_txt} mm). "
            f"Over-expanded openings expose neighboring copper and risk solder bridging."
        )
        violations.append(Violation(severity=sev, message=omsg, location=max_loc))

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        severity="info",  # Default value, will be overridden by finalize()
        status=status,
        score=score,
        metric=MetricResult.geometry_mm(
            measured_mm=measured,
            target_mm=recommended_min,
            limit_low_mm=absolute_min,
            limit_high_mm=absolute_max,
        ),
        violations=violations,
    ).finalize()
