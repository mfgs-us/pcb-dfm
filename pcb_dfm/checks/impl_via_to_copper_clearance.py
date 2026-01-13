from __future__ import annotations

import math
from collections import defaultdict
from math import floor
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

from ..geometry import queries
from ..geometry.primitives import Bounds
from ..results import CheckResult, Violation, ViolationLocation
from ..engine.context import CheckContext
from ..engine.check_runner import register_check
from ..ingest import GerberFileInfo

try:
    import gerber
except Exception:
    gerber = None

_INCH_TO_MM = 25.4
MAX_REPORTED_VIOLATIONS = 100


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


@dataclass
class DrillHit:
    x_mm: float
    y_mm: float
    d_mm: float


def _cell_key(x: float, y: float, cell: float) -> Tuple[int, int]:
    return (int(floor(x / cell)), int(floor(y / cell)))


def _get_board_bounds(ctx: CheckContext) -> Optional[Bounds]:
    """
    Best-effort board bounds. Prefer ctx.geometry.bounds() if available,
    else ctx.geometry.outline.bounds() if outline exists.
    """
    geom = ctx.geometry
    try:
        if hasattr(geom, "bounds"):
            b = geom.bounds()
            if b is not None:
                return b
    except Exception:
        pass

    try:
        outline = getattr(geom, "outline", None)
        if outline is not None and hasattr(outline, "bounds"):
            return outline.bounds()
    except Exception:
        pass

    return None


def _dist_bbox_to_bounds_edge_mm(b: Bounds, board: Bounds) -> float:
    """
    Returns minimum distance from bbox b to the board boundary edges, assuming b is inside.
    If b touches or extends to an edge, distance is 0.
    """
    left = max(0.0, b.min_x - board.min_x)
    right = max(0.0, board.max_x - b.max_x)
    bottom = max(0.0, b.min_y - board.min_y)
    top = max(0.0, board.max_y - b.max_y)
    return min(left, right, bottom, top)


def _center_to_bbox_distance_and_closest_point(cx: float, cy: float, b: Bounds) -> tuple[float, float, float]:
    """
    Returns (dist_center_to_bbox, closest_x, closest_y) where closest point lies on/in bbox.
    If center is inside bbox, dist is 0 and closest point is (cx,cy).
    """
    if cx < b.min_x:
        closest_x = b.min_x
    elif cx > b.max_x:
        closest_x = b.max_x
    else:
        closest_x = cx

    if cy < b.min_y:
        closest_y = b.min_y
    elif cy > b.max_y:
        closest_y = b.max_y
    else:
        closest_y = cy

    dx = closest_x - cx
    dy = closest_y - cy
    dist = math.hypot(dx, dy)
    return dist, closest_x, closest_y


def _via_edge_point_toward_target(
    cx: float, cy: float, r: float, tx: float, ty: float
) -> tuple[float, float]:
    """
    Point on via edge in direction of target point (tx,ty).
    If target equals center, returns center (degenerate).
    """
    vx = tx - cx
    vy = ty - cy
    norm = math.hypot(vx, vy)
    if norm <= 1e-12:
        return cx, cy
    ux = vx / norm
    uy = vy / norm
    return cx + r * ux, cy + r * uy


@register_check("via_to_copper_clearance")
def run_via_to_copper_clearance(ctx: CheckContext) -> CheckResult:
    """
    Approximate via-to-copper clearance by:

      - Using all drill hits as via centers
      - For each via, computing min distance from via edge to any copper polygon bounds

    Improvements over older version:
      - Perimeter copper band exclusion (optional)
      - Pad exclusion based on *distance* to copper bbox (instead of bbox containment)
      - Violation marker placed between via edge and closest copper point (more clear)

    Metric:
      measured_value: min_via_to_copper_clearance_mm
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", metric_cfg.get("unit", "mm"))

    limits = ctx.check_def.limits or {}
    recommended_min = float(limits.get("recommended_min", 0.15))
    absolute_min = float(limits.get("absolute_min", 0.1))

    raw_cfg = getattr(ctx.check_def, "raw", None) or {}

    # Raw configuration parameters for via-centric optimization
    via_max_d_mm = float(raw_cfg.get("via_max_d_mm", 0.8))
    include_component_pth = bool(raw_cfg.get("include_component_pth", False))
    
    # Ignore copper polygons close to board perimeter (band inwards from outline)
    perimeter_ignore_mm = float(raw_cfg.get("perimeter_ignore_mm", 0.0))

    # Replace old "bbox contains center" skip with a tighter pad exclusion radius.
    # This is a heuristic for "that's the via's own annular ring / pad copper".
    assumed_annular_ring_mm = float(raw_cfg.get("assumed_annular_ring_mm", 0.15))
    pad_exclusion_margin_mm = float(raw_cfg.get("pad_exclusion_margin_mm", 0.05))

    copper_layers = queries.get_copper_layers(ctx.geometry)

    # Filter to plated drill files only (via-centric approach)
    drill_files: List[GerberFileInfo] = [
        f for f in ctx.ingest.files 
        if f.layer_type == "drill" and getattr(f, "is_plated", None) is True
    ]

    if gerber is None or not drill_files or not copper_layers:
        viol = Violation(
            severity="warning",
            message="Cannot compute via-to-copper clearance (missing drill parser, drills, or copper).",
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

    hits: List[DrillHit] = []
    for info in drill_files:
        file_hits = _extract_drill_hits_mm(info.path, via_max_d_mm, include_component_pth)
        hits.extend(file_hits)

    if not hits:
        viol = Violation(
            severity="warning",
            message="No drill hits found to compute via-to-copper clearance.",
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

    board_bounds = _get_board_bounds(ctx)

    # Precompute copper bbox entries once (huge speed win)
    # entry: (layer_name, bounds, edge_dist_mm_or_None)
    copper_entries: List[tuple[str, Bounds, Optional[float]]] = []
    for layer in copper_layers:
        lname = layer.logical_layer
        for poly in layer.polygons:
            b: Bounds = poly.bounds()
            edge_dist = None
            if board_bounds is not None and perimeter_ignore_mm > 0.0:
                try:
                    edge_dist = _dist_bbox_to_bounds_edge_mm(b, board_bounds)
                except Exception:
                    edge_dist = None
            copper_entries.append((lname, b, edge_dist))

    if not copper_entries:
        viol = Violation(
            severity="warning",
            message="No copper polygons found to compute via-to-copper clearance.",
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

    # Spatial grid over copper bboxes with cell size proportional to search radius
    # This makes the runtime proportional to local copper density, not board area
    cell = max(0.25, min(1.0, recommended_min * 2.0))
    grid: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for idx, (_, b, _) in enumerate(copper_entries):
        # insert bbox into all intersected cells
        ix0 = int(floor(b.min_x / cell))
        ix1 = int(floor(b.max_x / cell))
        iy0 = int(floor(b.min_y / cell))
        iy1 = int(floor(b.max_y / cell))
        for iy in range(iy0, iy1 + 1):
            for ix in range(ix0, ix1 + 1):
                grid[(ix, iy)].append(idx)

    min_clear: Optional[float] = None
    worst_location: Optional[ViolationLocation] = None

    # (clearance_mm, layer_name, via_x_mm, via_y_mm, marker_x_mm, marker_y_mm)
    offenders: List[tuple[float, str, float, float, float, float]] = []

    for hit in hits:
        cx = hit.x_mm
        cy = hit.y_mm
        r = hit.d_mm / 2.0

        # Exclusion radius for copper considered to be "this via's own pad/annular ring"
        pad_exclusion_r = r + assumed_annular_ring_mm + pad_exclusion_margin_mm
        
        # Compute search radius based on clearance thresholds
        # Only need to search up to recommended_min + exclusion radius + small safety margin
        search_radius = pad_exclusion_r + recommended_min + 0.05  # 0.05mm safety margin
        
        # Compute grid neighborhood bounds
        ci, cj = _cell_key(cx, cy, cell)
        dk = int(math.ceil(search_radius / cell))
        
        # Track seen indices to avoid duplicates
        seen: set[int] = set()
        
        # Scan bounded neighborhood (constant-ish time per via)
        for di in range(-dk, dk + 1):
            for dj in range(-dk, dk + 1):
                bucket = grid.get((ci + di, cj + dj))
                if not bucket:
                    continue

                for idx in bucket:
                    if idx in seen:
                        continue
                    seen.add(idx)

                    layer_name, b, edge_dist = copper_entries[idx]

                    if perimeter_ignore_mm > 0.0 and edge_dist is not None and edge_dist <= perimeter_ignore_mm:
                        continue

                    dist_center, closest_x, closest_y = _center_to_bbox_distance_and_closest_point(cx, cy, b)

                    # Skip if within pad exclusion radius
                    if dist_center <= pad_exclusion_r:
                        continue
                        
                    # Skip if beyond search radius (early exit optimization)
                    if dist_center - r > search_radius:
                        continue

                    clearance = dist_center - r
                    if clearance < 0.0:
                        clearance = 0.0

                    vx_edge_x, vx_edge_y = _via_edge_point_toward_target(cx, cy, r, closest_x, closest_y)
                    marker_x = 0.5 * (vx_edge_x + closest_x)
                    marker_y = 0.5 * (vx_edge_y + closest_y)

                    if min_clear is None or clearance < min_clear:
                        min_clear = clearance
                        worst_location = ViolationLocation(
                            layer=layer_name,
                            x_mm=marker_x,
                            y_mm=marker_y,
                            notes="Midpoint between via edge and nearest copper bbox point (approx).",
                        )

                    if clearance < recommended_min:
                        offenders.append((clearance, layer_name, cx, cy, marker_x, marker_y))
                    
                    # Early exit per via: if we found clearance at absolute_min or below, no need to search further
                    if clearance <= absolute_min:
                        break
                
                # Early exit if we already found the worst possible case
                if min_clear is not None and min_clear <= absolute_min:
                    break
            
            # Early exit if we already found the worst possible case
            if min_clear is not None and min_clear <= absolute_min:
                break
        
        # Global early exit: if we already hit absolute minimum, no need to check more vias
        if min_clear is not None and min_clear <= absolute_min:
            break

    if min_clear is None:
        viol = Violation(
            severity="warning",
            message="Could not determine via-to-copper clearance (all copper filtered or no measurable candidates).",
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
    if min_clear < absolute_min:
        status = "fail"
        severity = "warning"
    elif min_clear < recommended_min:
        status = "warning"
        severity = "warning"
    else:
        status = "pass"
        severity = ctx.check_def.severity or "error"

    # Score
    if min_clear >= recommended_min:
        score = 100.0
    elif min_clear <= absolute_min:
        score = 0.0
    else:
        span = max(1e-12, recommended_min - absolute_min)
        score = max(0.0, min(100.0, 100.0 * (min_clear - absolute_min) / span))

    margin_to_limit = float(min_clear - absolute_min)

    violations: List[Violation] = []
    if status != "pass":
        offenders_sorted = sorted(offenders, key=lambda t: t[0])
        if offenders_sorted:
            for clearance, layer_name, vx, vy, mx, my in offenders_sorted[:MAX_REPORTED_VIOLATIONS]:
                msg = (
                    f"Minimum via-to-copper clearance {clearance:.3f} mm on layer {layer_name} is below "
                    f"recommended {recommended_min:.3f} mm (absolute minimum {absolute_min:.3f} mm)."
                )
                violations.append(
                    Violation(
                        severity=severity,
                        message=msg,
                        location=ViolationLocation(
                            layer=layer_name,
                            x_mm=mx,
                            y_mm=my,
                            notes="Midpoint between via edge and nearest copper bbox point (approx).",
                        ),
                    )
                )
        else:
            msg = (
                f"Minimum via-to-copper clearance {min_clear:.3f} mm is below "
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
            "measured_value": float(min_clear),
            "target": recommended_min,
            "limit_low": absolute_min,
            "limit_high": None,
            "margin_to_limit": margin_to_limit,
        },
        violations=violations,
    )


def _extract_drill_hits_mm(path, via_max_d_mm: float = 0.8, include_component_pth: bool = False) -> List[DrillHit]:
    if gerber is None:
        return []
    try:
        drill_layer = gerber.read(str(path))
    except Exception:
        return []

    # Detect units first to avoid incorrect conversion
    units = _detect_excellon_units(drill_layer)
    
    # Only convert to inch if the file is in mm and we need inch for internal processing
    if units == 'mm':
        try:
            drill_layer.to_inch()
        except Exception:
            # If conversion fails, assume coordinates are already in inch
            pass
    elif units == 'inch':
        # Already in inch, no conversion needed
        pass

    hits_out: List[DrillHit] = []

    hits = getattr(drill_layer, "hits", None)
    if hits is None:
        return hits_out

    for hit in hits:
        x_in = y_in = d_in = None

        # New-style API
        try:
            if hasattr(hit, "x") and hasattr(hit, "y"):
                x_in = float(hit.x)
                y_in = float(hit.y)
            elif hasattr(hit, "position"):
                px, py = hit.position
                x_in = float(px)
                y_in = float(py)

            tool = getattr(hit, "tool", None)
            if tool is not None and hasattr(tool, "diameter"):
                d_in = float(tool.diameter)
        except Exception:
            pass

        # Old-style (tool, (x, y))
        if x_in is None or y_in is None or d_in is None:
            try:
                tool, (px, py) = hit
                x_in = float(px)
                y_in = float(py)
                d_in = float(getattr(tool, "diameter"))
            except Exception:
                continue

        # Convert to mm (always multiply by 25.4 since we're working in inch internally)
        x_mm = x_in * _INCH_TO_MM
        y_mm = y_in * _INCH_TO_MM
        d_mm = d_in * _INCH_TO_MM
        
        # Filter by diameter for via-like drills only
        if d_mm > via_max_d_mm:
            continue
            
        # Additional filtering for component PTH if disabled
        if not include_component_pth and d_mm > 0.8:  # Typical component drill threshold
            continue

        hits_out.append(
            DrillHit(
                x_mm=x_mm,
                y_mm=y_mm,
                d_mm=d_mm,
            )
        )

    return hits_out
