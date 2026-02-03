from __future__ import annotations

from dataclasses import dataclass
from math import floor, sqrt
from typing import List, Optional
from collections import defaultdict
import re

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, Violation, ViolationLocation, MetricResult
from ..geometry import queries

# pcb-tools excellon reader (optional)
try:
    from gerber import excellon  # type: ignore
except Exception:
    excellon = None  # type: ignore

_INCH_TO_MM = 25.4


@dataclass
class DrillHole:
    x_mm: float
    y_mm: float
    diameter_mm: float
    is_plated: bool = True


def _detect_excellon_units(drill_file) -> str:
    """Detect Excellon file units from header."""
    # Try to get units from the file object
    if hasattr(drill_file, 'units'):
        units = getattr(drill_file, 'units', None)
        if units in ('inch', 'mm'):
            return units
    
    # Try to detect from header/comments
    if hasattr(drill_file, 'header'):
        header = getattr(drill_file, 'header', '')
        if isinstance(header, str):
            if 'M71' in header.upper():
                return 'mm'
            elif 'M72' in header.upper():
                return 'inch'
    
    # Default to inch if undetectable
    return 'inch'


def _point_in_polygon(x: float, y: float, vertices: List) -> bool:
    """Ray-casting algorithm for point-in-polygon test."""
    if len(vertices) < 3:
        return False
    
    inside = False
    n = len(vertices)
    for i in range(n):
        j = (i + 1) % n
        xi, yi = vertices[i].x, vertices[i].y
        xj, yj = vertices[j].x, vertices[j].y
        
        # Check if point is on an edge (considered inside)
        if _point_on_segment(x, y, xi, yi, xj, yj):
            return True
        
        # Ray-casting test
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
    
    return inside


def _point_on_segment(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> bool:
    """Check if point (px, py) lies on line segment (x1,y1)-(x2,y2)."""
    # Cross product to check collinearity
    cross = (py - y1) * (x2 - x1) - (px - x1) * (y2 - y1)
    if abs(cross) > 1e-10:  # Not collinear
        return False
    
    # Check if point is within segment bounds
    dot = (px - x1) * (px - x2) + (py - y1) * (py - y2)
    if dot > 1e-10:  # Outside segment bounds
        return False
    
    return True


def _distance_point_to_segment(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    """Calculate minimum distance from point to line segment."""
    dx = x2 - x1
    dy = y2 - y1
    
    if abs(dx) < 1e-10 and abs(dy) < 1e-10:
        # Segment is a point
        return sqrt((px - x1)**2 + (py - y1)**2)
    
    # Parameter t determines closest point on infinite line
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    
    if t < 0:
        # Closest point is segment start
        return sqrt((px - x1)**2 + (py - y1)**2)
    elif t > 1:
        # Closest point is segment end
        return sqrt((px - x2)**2 + (py - y2)**2)
    else:
        # Closest point is interior to segment
        closest_x = x1 + t * dx
        closest_y = y1 + t * dy
        return sqrt((px - closest_x)**2 + (py - closest_y)**2)


def _min_distance_to_polygon_edges(x: float, y: float, vertices: List) -> float:
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


def _is_pad_like_polygon(poly, drill_diameter_mm: float, absolute_min: float) -> bool:
    """Filter out non-pad copper polygons (1A)."""
    b = poly.bounds()
    if b is None:
        return False
    
    width = b.max_x - b.min_x
    height = b.max_y - b.min_y
    
    if width <= 0 or height <= 0:
        return False
    
    # Exclude traces/regions with high aspect ratio
    aspect_ratio = max(width, height) / min(width, height)
    if aspect_ratio > 2.5:
        return False
    
    # Exclude polygons too small to contain required annular ring
    min_required_size = drill_diameter_mm + 2 * absolute_min - 0.001  # small_eps
    if min(width, height) < min_required_size:
        return False
    
    # Exclude huge polygons (likely planes)
    max_pad_size = 10.0  # 10mm max for typical pads
    diagonal = sqrt(width**2 + height**2)
    if diagonal > max_pad_size:
        return False
    
    return True


def _collect_drills_from_excellon(ctx: CheckContext) -> List[DrillHole]:
    """Collect plated drills from Excellon files if available."""
    if excellon is None:
        return []

    drills: List[DrillHole] = []
    for f in ctx.ingest.files:
        if f.layer_type != "drill":
            continue
        if f.format != "excellon":
            continue
        
        # 1E) Only use plated drills, exclude NPTH
        if f.logical_layer == "DrillNonPlated" or f.is_plated is False:
            continue

        try:
            drill_file = excellon.read(str(f.path))
        except Exception:
            continue

        # 1D) Detect units and only convert if needed
        units = _detect_excellon_units(drill_file)
        
        # Only convert to inch if the file is in mm and we need inch for internal processing
        if units == 'mm':
            # Convert mm to inch for internal processing, then to mm at the end
            try:
                drill_file.to_inch()
            except Exception:
                # If conversion fails, assume coordinates are already in inch
                pass
        elif units == 'inch':
            # Already in inch, no conversion needed
            pass
        else:
            # Unknown units, try to convert as fallback
            try:
                drill_file.to_inch()
            except Exception:
                pass

        hits = getattr(drill_file, "hits", [])
        for hit in hits:
            x = y = d = None
            # new-style DrillHit
            try:
                if hasattr(hit, "x") and hasattr(hit, "y"):
                    x = float(hit.x)
                    y = float(hit.y)
                elif hasattr(hit, "position"):
                    px, py = hit.position  # type: ignore[attr-defined]
                    x = float(px)
                    y = float(py)

                tool = getattr(hit, "tool", None)
                if tool is not None and hasattr(tool, "diameter"):
                    d = float(tool.diameter)
            except Exception:
                pass

            # old-style (tool, (x, y)) tuple
            if x is None or y is None or d is None:
                try:
                    tool, (px, py) = hit  # type: ignore[misc]
                    x = float(px)
                    y = float(py)
                    d = float(tool.diameter)
                except Exception:
                    continue

            # Store diameter_mm directly with proper unit conversion
            if units == 'mm':
                # File was originally in mm, coordinates are now in inch after to_inch()
                # Convert back to mm
                diameter_mm = d * _INCH_TO_MM
            else:
                # File was in inch, coordinates are in inch
                diameter_mm = d * _INCH_TO_MM
            
            drills.append(
                DrillHole(
                    x_mm=x * _INCH_TO_MM,
                    y_mm=y * _INCH_TO_MM,
                    diameter_mm=diameter_mm,
                    is_plated=True,
                )
            )

    return drills


@register_check("min_annular_ring")
def run_min_annular_ring(ctx: CheckContext) -> CheckResult:
    """
    Estimate minimum annular ring for plated drills.

    Improved Approach:
      - Collect only plated drills from Excellon files (proper unit detection).
      - Filter copper polygons to pad-like shapes (exclude traces, planes).
      - Use point-in-polygon test instead of bbox containment.
      - Compute true annular ring as distance from drill edge to polygon edge.
      - Report minimum ring across all valid pad/drill combinations.

    Units: mm.
    """
    metric_cfg = ctx.check_def.metric or {}
    units_raw = metric_cfg.get("units", metric_cfg.get("unit", "mm"))
    units = "mm" if units_raw in (None, "", "mm", "um") else units_raw

    limits = ctx.check_def.limits or {}
    recommended_min = float(limits.get("recommended_min", 0.1))   # mm
    absolute_min = float(limits.get("absolute_min", 0.075))       # mm

    drills = _collect_drills_from_excellon(ctx)
    copper_layers = queries.get_copper_layers(ctx.geometry)

    if not drills or not copper_layers:
        viol = Violation(
            severity="warning",
            message="Cannot compute annular ring (missing drills or copper geometry).",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="warning",
            severity="info",  # Default value, will be overridden by finalize()
            score=50.0,
            metric=MetricResult(
                kind="geometry",
                units=units,
                measured_value=None,
                target=recommended_min,
                limit_low=absolute_min,
                limit_high=None,
                margin_to_limit=None,
            ),
            violations=[viol],
        ).finalize()

    min_ring: Optional[float] = None
    worst_location: Optional[ViolationLocation] = None

    # 1A) Filter copper polygons to pad-like shapes only
    pad_candidates = []
    for layer in copper_layers:
        for poly in layer.polygons:
            # Use the smallest drill diameter for filtering (conservative approach)
            min_drill_dia = min(d.diameter_mm for d in drills)
            if _is_pad_like_polygon(poly, min_drill_dia, absolute_min):
                pad_candidates.append((poly, layer.logical_layer))

    if not pad_candidates:
        viol = Violation(
            severity="warning",
            message="No pad-like copper features found around drills to compute annular ring.",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="warning",
            severity="info",  # Default value, will be overridden by finalize()
            score=50.0,
            metric=MetricResult(
                kind="geometry",
                units=units,
                measured_value=None,
                target=recommended_min,
                limit_low=absolute_min,
                limit_high=None,
                margin_to_limit=None,
            ),
            violations=[viol],
        ).finalize()

    # Build spatial grid for efficient candidate selection
    cell = max(0.5, max(d.diameter_mm for d in drills))
    grid = defaultdict(list)

    for idx, (poly, layer_name) in enumerate(pad_candidates):
        b = poly.bounds()
        ix0 = int(floor(b.min_x / cell))
        ix1 = int(floor(b.max_x / cell))
        iy0 = int(floor(b.min_y / cell))
        iy1 = int(floor(b.max_y / cell))
        for iy in range(iy0, iy1 + 1):
            for ix in range(ix0, ix1 + 1):
                grid[(ix, iy)].append(idx)

    # Check each drill against nearby pad candidates
    for hole in drills:
        r_drill = hole.diameter_mm * 0.5

        ci = int(floor(hole.x_mm / cell))
        cj = int(floor(hole.y_mm / cell))

        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for idx in grid.get((ci + di, cj + dj), []):
                    poly, layer_name = pad_candidates[idx]
                    
                    # 1B) Use point-in-polygon test instead of bbox containment
                    if not _point_in_polygon(hole.x_mm, hole.y_mm, poly.vertices):
                        continue

                    # 1C) Compute true annular ring as distance from drill edge to polygon edge
                    min_dist_to_edge = _min_distance_to_polygon_edges(hole.x_mm, hole.y_mm, poly.vertices)
                    ring = min_dist_to_edge - r_drill
                    
                    # Clamp to 0 (negative ring means drill extends beyond pad)
                    if ring < 0.0:
                        ring = 0.0

                    if min_ring is None or ring < min_ring:
                        min_ring = ring
                        worst_location = ViolationLocation(
                            layer=layer_name,
                            x_mm=hole.x_mm,
                            y_mm=hole.y_mm,
                            notes="True annular ring computed from drill edge to polygon edge.",
                        )

    if min_ring is None:
        viol = Violation(
            severity="warning",
            message="No valid pad-drill combinations found to compute annular ring.",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="warning",
            severity="info",  # Default value, will be overridden by finalize()
            score=50.0,
            metric=MetricResult(
                kind="geometry",
                units=units,
                measured_value=None,
                target=recommended_min,
                limit_low=absolute_min,
                limit_high=None,
                margin_to_limit=None,
            ),
            violations=[viol],
        ).finalize()

    # status only (severity handled by finalize)
    if min_ring < absolute_min:
        status = "fail"
    elif min_ring < recommended_min:
        status = "warning"
    else:
        status = "pass"

    # score
    if min_ring >= recommended_min:
        score = 100.0
    elif min_ring <= absolute_min:
        score = 0.0
    else:
        span = recommended_min - absolute_min
        score = max(0.0, min(100.0, 100.0 * (min_ring - absolute_min) / span))

    margin_to_limit = float(min_ring - absolute_min)

    violations: List[Violation] = []
    if status != "pass":
        # Determine severity based on status
        severity = "error" if status == "fail" else "warning"
        
        msg = (
            f"Minimum annular ring {min_ring:.3f} mm is below "
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
        severity="info",  # Default value, will be overridden by finalize()
        status=status,
        score=score,
        metric=MetricResult(
            kind="geometry",
            units=units,
            measured_value=float(min_ring),
            target=recommended_min,
            limit_low=absolute_min,
            limit_high=None,
            margin_to_limit=margin_to_limit,
        ),
        violations=violations,
    ).finalize()
