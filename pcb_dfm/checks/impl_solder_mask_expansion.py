from __future__ import annotations

import math
from collections import defaultdict
from math import floor
from typing import List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, Violation, ViolationLocation, MetricResult
from ..geometry.primitives import Polygon


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
    
    Returns:
        - List of normalized opening polygons
        - String indicating polarity: "openings" or "coverage" 
    """
    if board_area <= 0:
        # No board outline available, assume openings (most common)
        return mask_polygons, "openings"
    
    # Calculate total mask polygon area
    total_mask_area = sum(_poly_area_mm2(poly) for poly in mask_polygons)
    
    # Heuristic: if mask area < 50% of board area, treat as openings
    if total_mask_area < 0.5 * board_area:
        return mask_polygons, "openings"
    else:
        # Treat as coverage and invert to get openings
        return _invert_mask_coverage(mask_polygons, board_area), "coverage"


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


def _intersects(a, b) -> bool:
    if hasattr(a, "intersects"):
        return bool(a.intersects(b))
    # fallback: if you have no boolean ops, you can't do this reliably
    return True


def _contains(a, b) -> bool:
    if hasattr(a, "contains"):
        return bool(a.contains(b))
    return False


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
    metric_cfg = ctx.check_def.metric or {}
    target_raw = metric_cfg.get("target", {}) or {}
    limits_raw = metric_cfg.get("limits", {}) or {}

    # Normalize geometry thresholds to mm
    units_raw = (metric_cfg.get("units") or "mm").lower()
    source_is_um = units_raw in ("um", "micron", "microns")

    if isinstance(target_raw, dict):
        raw_target_min = target_raw.get("min", 50.0 if source_is_um else 0.05)
    else:
        raw_target_min = 50.0 if source_is_um else 0.05

    if isinstance(limits_raw, dict):
        raw_abs_min = limits_raw.get("min", 30.0 if source_is_um else 0.03)
    else:
        raw_abs_min = 30.0 if source_is_um else 0.03

    scale = 0.001 if source_is_um else 1.0  # um -> mm if needed
    recommended_min = float(raw_target_min) * scale
    absolute_min = float(raw_abs_min) * scale

    # Raw parameters
    raw_cfg = getattr(ctx.check_def, "raw", None) or {}
    pad_min_area_mm2 = float(raw_cfg.get("pad_min_area_mm2", 0.02))
    pad_max_area_mm2 = float(raw_cfg.get("pad_max_area_mm2", 4.0))
    pad_max_aspect_ratio = float(raw_cfg.get("pad_max_aspect_ratio", 10.0))
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

    # Spatial index for mask openings to avoid pads*masks scans
    cell = max(0.5, mask_search_inflate_mm * 4.0)  # mm, tuned for typical mask clearances
    mask_grid_by_side: dict[str, dict[tuple[int, int], list[int]]] = {}
    masks_by_side: dict[str, list[_MaskOpening]] = defaultdict(list)

    for m in masks:
        key = str(m.side).lower() if m.side is not None else "unknown"
        masks_by_side[key].append(m)

    for side_key, side_masks in masks_by_side.items():
        grid = defaultdict(list)
        for idx, m in enumerate(side_masks):
            # Put each mask bbox into all cells it overlaps
            ix0 = int(floor((m.min_x - mask_search_inflate_mm) / cell))
            ix1 = int(floor((m.max_x + mask_search_inflate_mm) / cell))
            iy0 = int(floor((m.min_y - mask_search_inflate_mm) / cell))
            iy1 = int(floor((m.max_y + mask_search_inflate_mm) / cell))
            for iy in range(iy0, iy1 + 1):
                for ix in range(ix0, ix1 + 1):
                    grid[(ix, iy)].append(idx)
        mask_grid_by_side[side_key] = grid

    # helper to fetch candidate masks for a pad
    def _mask_candidates_for_pad(pad: _Pad) -> list[_MaskOpening]:
        side_key = str(pad.side).lower() if pad.side is not None else "unknown"
        side_masks = masks_by_side.get(side_key, [])
        grid = mask_grid_by_side.get(side_key)
        if not grid:
            return []
        ci = int(floor(pad.cx / cell))
        cj = int(floor(pad.cy / cell))
        out: list[_MaskOpening] = []
        seen: set[int] = set()
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for midx in grid.get((ci + di, cj + dj), []):
                    if midx in seen:
                        continue
                    seen.add(midx)
                    out.append(side_masks[midx])
        return out

    min_expansion = math.inf
    min_loc: Optional[ViolationLocation] = None
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
        
        # 3B) Compute expansion using true distance measurement, not bbox approximation
        if m.polarity == "coverage":
            # For coverage polygons, expansion is negative (copper intrudes into mask)
            # This is a simplified approach - proper implementation would need boolean ops
            expansion = -0.1  # Placeholder negative value
            notes = "Coverage-based mask (negative expansion estimated)"
        else:
            # For opening polygons, compute true distance from pad boundary to mask boundary
            try:
                min_distance = _min_distance_between_polygons(pad.poly, m.poly)
                
                # Expansion is positive if mask opening extends beyond copper
                # Negative if copper extends beyond mask opening
                if _contains(m.poly, pad.poly):
                    # Mask fully contains pad, expansion is positive
                    expansion = min_distance
                else:
                    # Partial overlap or no containment, expansion could be negative
                    expansion = -min_distance if min_distance < 1.0 else min_distance
                
                notes = "True distance-based expansion measurement"
            except Exception:
                # Fallback to bbox approximation if distance calculation fails
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

    # Determine status only (severity handled by finalize)
    if measured >= recommended_min:
        status = "pass"
        score = 100.0
    elif measured < absolute_min:
        status = "fail"
        score = 0.0
    else:
        status = "warning"
        span = max(1e-6, recommended_min - absolute_min)
        frac = (measured - absolute_min) / span
        score = max(0.0, min(100.0, 60.0 + 40.0 * max(0.0, frac)))

    margin_to_limit = float(measured - absolute_min)

    msg = (
        f"Minimum solder mask expansion is {measured:.3f} mm "
        f"(recommended >= {recommended_min:.3f} mm, absolute >= {absolute_min:.3f} mm)."
    )

    violations: List[Violation] = []
    if status != "pass":
        violation_severity = "warning" if status == "warning" else "error"
        violations.append(
            Violation(
                severity=violation_severity,
                message=msg,
                location=min_loc,
            )
        )

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
        ),
        violations=violations,
    ).finalize()
