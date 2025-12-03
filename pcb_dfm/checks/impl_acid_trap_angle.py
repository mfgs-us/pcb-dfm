from __future__ import annotations

import math
from typing import List, Optional

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, Violation, ViolationLocation


def _iter_vertices_mm(poly) -> List[tuple[float, float]]:
    """
    Extract polygon vertices in mm.

    Handles:
    - Shapely like polygons (poly.exterior.coords)
    - Our own polygons with .vertices as a list of Point2D objects (x, y)
    - Fallback to tuples/lists or generic iterables
    """
    # Shapely style
    if hasattr(poly, "exterior") and hasattr(poly.exterior, "coords"):
        coords = list(poly.exterior.coords)
        if len(coords) >= 2 and coords[0] == coords[-1]:
            coords = coords[:-1]
        return [(float(x), float(y)) for (x, y) in coords]

    # Our geometry wrapper: poly.vertices -> list of Point2D or tuples
    if hasattr(poly, "vertices"):
        pts: List[tuple[float, float]] = []
        for p in poly.vertices:
            if hasattr(p, "x") and hasattr(p, "y"):
                pts.append((float(p.x), float(p.y)))
            elif isinstance(p, (tuple, list)) and len(p) >= 2:
                pts.append((float(p[0]), float(p[1])))
        return pts

    # Alternative naming
    if hasattr(poly, "points"):
        pts: List[tuple[float, float]] = []
        for p in poly.points:
            if hasattr(p, "x") and hasattr(p, "y"):
                pts.append((float(p.x), float(p.y)))
            elif isinstance(p, (tuple, list)) and len(p) >= 2:
                pts.append((float(p[0]), float(p[1])))
        return pts

    # Generic fallback
    pts = list(poly)
    if pts and isinstance(pts[0], (tuple, list)) and len(pts[0]) >= 2:
        return [(float(p[0]), float(p[1])) for p in pts]

    return []


def _poly_area_mm2(poly) -> float:
    """
    Best effort polygon area in mm^2, matching other geometry helpers.
    """
    if hasattr(poly, "area_mm2"):
        return float(poly.area_mm2)

    if hasattr(poly, "area"):
        try:
            return float(poly.area())
        except TypeError:
            try:
                return float(poly.area)
            except TypeError:
                pass

    # Fallback: bbox area
    b = poly.bounds()
    # Expecting something like obj with min_x, max_x, min_y, max_y
    try:
        width = b.max_x - b.min_x
        height = b.max_y - b.min_y
        return max(0.0, float(width) * float(height))
    except Exception:
        return 0.0


@register_check("acid_trap_angle")
def run_acid_trap_angle(ctx: CheckContext) -> CheckResult:
    """
    Detect sharp copper corners (small interior angles) that can act as acid traps.

    First pass:
    - Works on all copper polygons.
    - Approximates interior angles at each vertex.
    - Flags smallest angle if below thresholds.

    Units: degrees.
    """

    metric_cfg = ctx.check_def.metric or {}
    target_raw = metric_cfg.get("target", {}) or {}
    limits_raw = metric_cfg.get("limits", {}) or {}

    # JSON usually:
    # "metric": {
    #   "kind": "angle",
    #   "units": "deg",
    #   "preferred_direction": "maximize",
    #   "target": { "min": 90.0 },
    #   "limits": { "min": 60.0 },
    #   ...
    # }
    if isinstance(target_raw, dict):
        recommended_min_deg = float(target_raw.get("min", 90.0))
    else:
        recommended_min_deg = float(target_raw or 90.0)

    if isinstance(limits_raw, dict):
        absolute_min_deg = float(limits_raw.get("min", 60.0))
    else:
        absolute_min_deg = float(limits_raw or 60.0)

    raw_cfg = getattr(ctx.check_def, "raw", None) or {}
    min_area_mm2 = float(raw_cfg.get("min_area_mm2", 0.002))      # ignore tiny crumbs
    max_area_mm2 = float(raw_cfg.get("max_area_mm2", 2000.0))     # optional large plane cutoff
    min_edge_length_mm = float(raw_cfg.get("min_edge_length_mm", 0.02))
    consider_planes = bool(raw_cfg.get("consider_planes", True))

    geom = ctx.geometry

    global_min_angle_deg = math.inf
    global_min_loc: Optional[ViolationLocation] = None

    for layer in getattr(geom, "layers", []):
        layer_type = getattr(layer, "layer_type", getattr(layer, "type", None))
        if layer_type != "copper":
            continue

        logical = getattr(layer, "logical_layer", getattr(layer, "name", None))

        for poly in getattr(layer, "polygons", []):
            area = _poly_area_mm2(poly)
            if area < min_area_mm2:
                continue
            if not consider_planes and area > max_area_mm2:
                continue

            pts = _iter_vertices_mm(poly)
            n = len(pts)
            if n < 3:
                continue

            for i in range(n):
                x0, y0 = pts[i - 1]
                x1, y1 = pts[i]
                x2, y2 = pts[(i + 1) % n]

                v1x = x0 - x1
                v1y = y0 - y1
                v2x = x2 - x1
                v2y = y2 - y1

                len1 = math.hypot(v1x, v1y)
                len2 = math.hypot(v2x, v2y)
                if len1 < min_edge_length_mm or len2 < min_edge_length_mm:
                    continue

                dot = v1x * v2x + v1y * v2y
                denom = max(1e-12, len1 * len2)
                cos_theta = max(-1.0, min(1.0, dot / denom))
                angle_rad = math.acos(cos_theta)
                angle_deg = math.degrees(angle_rad)

                if angle_deg < global_min_angle_deg:
                    global_min_angle_deg = angle_deg
                    global_min_loc = ViolationLocation(
                        layer=logical,
                        x_mm=x1,
                        y_mm=y1,
                        notes="Sharpest copper corner (approximate).",
                    )

    # No corners found
    if not math.isfinite(global_min_angle_deg):
        viol = Violation(
            severity="info",
            message="No eligible copper corners found to evaluate acid trap angles.",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            severity=ctx.check_def.severity or ctx.check_def.severity_default,
            status="pass",
            score=100.0,
            metric={
                "kind": "angle",
                "units": "deg",
                "measured_value": None,
                "target": recommended_min_deg,
                "limit_low": absolute_min_deg,
                "limit_high": None,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    measured = float(global_min_angle_deg)

    if measured >= recommended_min_deg:
        status = "pass"
        severity = ctx.check_def.severity or ctx.check_def.severity_default
        score = 100.0
    elif measured < absolute_min_deg:
        status = "fail"
        severity = "error"
        score = 0.0
    else:
        status = "warning"
        severity = "warning"
        span = max(1e-6, recommended_min_deg - absolute_min_deg)
        frac = (measured - absolute_min_deg) / span
        score = max(0.0, min(100.0, 60.0 + 40.0 * max(0.0, frac)))

    margin_to_limit = float(measured - absolute_min_deg)

    msg = (
        f"Smallest copper corner angle is {measured:.1f}° "
        f"(recommended >= {recommended_min_deg:.1f}°, absolute >= {absolute_min_deg:.1f}°)."
    )

    violations: List[Violation] = []
    if status != "pass":
        violations.append(
            Violation(
                severity=severity,
                message=msg,
                location=global_min_loc,
            )
        )
    else:
        violations.append(
            Violation(
                severity="info",
                message=msg,
                location=global_min_loc,
            )
        )

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        severity=ctx.check_def.severity or ctx.check_def.severity_default,
        status=status,
        score=score,
        metric={
            "kind": "angle",
            "units": "deg",
            "measured_value": measured,
            "target": recommended_min_deg,
            "limit_low": absolute_min_deg,
            "limit_high": None,
            "margin_to_limit": margin_to_limit,
        },
        violations=violations,
    )
