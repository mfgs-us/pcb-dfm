from __future__ import annotations

from typing import List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry import queries
from ..results import CheckResult, Violation, ViolationLocation


def _poly_vertices(poly) -> List[Tuple[float, float]]:
    """Extract (x, y) vertices in mm from a geometry Polygon."""
    verts = getattr(poly, "vertices", None)
    if not verts:
        return []
    pts: List[Tuple[float, float]] = []
    for p in verts:
        if hasattr(p, "x") and hasattr(p, "y"):
            pts.append((float(p.x), float(p.y)))
        elif isinstance(p, (tuple, list)) and len(p) >= 2:
            pts.append((float(p[0]), float(p[1])))
    return pts


def _shoelace_area(pts: List[Tuple[float, float]]) -> float:
    """Absolute polygon area (mm^2) via the shoelace formula."""
    n = len(pts)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


def _longest_extent(pts: List[Tuple[float, float]], bbox_diag: float) -> float:
    """
    Longest caliper extent (diameter) of the vertex set in mm.

    This is the maximum pairwise vertex distance, which -- unlike a bounding-box
    dimension -- is rotation invariant. For small polygons we compute it exactly;
    for very dense rings we fall back to the bbox diagonal (also an upper bound on
    any axis-aligned dimension) to keep the cost bounded.
    """
    n = len(pts)
    if n < 2:
        return 0.0
    if n > 256:
        return bbox_diag
    max_d2 = 0.0
    for i in range(n):
        xi, yi = pts[i]
        for j in range(i + 1, n):
            xj, yj = pts[j]
            dx = xi - xj
            dy = yi - yj
            d2 = dx * dx + dy * dy
            if d2 > max_d2:
                max_d2 = d2
    return max_d2 ** 0.5


def _estimate_width_mm(poly, short_dim: float, bbox_diag: float) -> float:
    """
    Estimate the true minimum width of an elongated copper polygon.

    The bounding-box short dimension only measures width correctly for
    axis-aligned rectangles; a diagonal or curved sliver has a large bbox short
    dimension yet a genuinely narrow body. For an elongated shape the mean width
    is well approximated by ``area / length`` (length = longest caliper extent),
    and both area and longest extent are rotation invariant, so a diagonal sliver
    measures the same as an axis-aligned one. We take the smaller of that estimate
    and the bbox short dimension so the result is never a worse over-estimate than
    the old bbox measure.
    """
    pts = _poly_vertices(poly)
    area = _shoelace_area(pts)
    length = _longest_extent(pts, bbox_diag)
    if area <= 0.0 or length <= 0.0:
        return short_dim
    mean_width = area / length
    return min(short_dim, mean_width)


@register_check("copper_sliver_width")
def run_copper_sliver_width(ctx: CheckContext) -> CheckResult:
    """
    Detect narrow copper "slivers".

    Candidate selection uses polygon bounding boxes (cheap pre-filter):
      - For each copper polygon:
          * compute bbox (width, height) in mm
          * bbox area = width * height
          * aspect_ratio = long_dim / short_dim
      - Consider as sliver candidates only if:
          * area >= min_area_mm2
          * aspect_ratio >= min_aspect_ratio
          * long_dim >= min_long_dim_mm
          * min_candidate_short_dim_mm <= short_dim <= max_short_dim_mm
          * area >= ignore_tiny_feature_area_mm2

    Width measurement (improved over bbox short-dimension):
      The reported sliver width is estimated as ``area / longest_extent`` (mean
      width of an elongated shape), clamped to be no larger than the bbox short
      dimension. Both area and longest extent are rotation invariant, so diagonal
      and curved slivers -- which a bbox short dimension mis-measures -- are
      handled correctly. Report the minimum estimated width across all candidates.
    """
    metric_cfg = ctx.check_def.metric or {}
    units_raw = metric_cfg.get("units", metric_cfg.get("unit", "mm"))
    units = "mm" if units_raw in (None, "", "mm", "um") else units_raw

    limits = ctx.check_def.limits or {}
    recommended_min = float(limits.get("recommended_min", 0.15))  # mm
    absolute_min = float(limits.get("absolute_min", 0.10))        # mm

    raw_cfg = ctx.check_def.raw or {}

    # Filtering thresholds (all in mm / mm^2)
    min_area_mm2 = float(raw_cfg.get("min_area_mm2", 0.02))
    min_aspect_ratio = float(raw_cfg.get("min_aspect_ratio", 4.0))
    min_long_dim_mm = float(raw_cfg.get("min_long_dim_mm", 0.5))
    max_short_dim_mm = float(raw_cfg.get("max_short_dim_mm", 0.3))
    ignore_tiny_feature_area_mm2 = float(raw_cfg.get("ignore_tiny_feature_area_mm2", 0.005))
    # New: lower bound on candidate short dimension so we ignore ultra tiny artifacts
    min_candidate_short_dim_mm = float(raw_cfg.get("min_candidate_short_dim_mm", 0.05))

    copper_layers = queries.get_copper_layers(ctx.geometry)
    if not copper_layers:
        viol = Violation(
            severity="warning",
            message="No copper layers available to compute copper sliver width.",
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

    min_sliver: Optional[float] = None
    worst_location: Optional[ViolationLocation] = None

    for layer in copper_layers:
        for poly in layer.polygons:
            b = poly.bounds()
            width = max(b.max_x - b.min_x, 0.0)
            height = max(b.max_y - b.min_y, 0.0)
            if width <= 0.0 or height <= 0.0:
                continue

            area = width * height
            if area < ignore_tiny_feature_area_mm2:
                # extremely small artifact, ignore entirely
                continue

            short_dim_bbox = min(width, height)
            if short_dim_bbox <= 0.0:
                continue
            bbox_diag = (width * width + height * height) ** 0.5

            # Use rotation-invariant width (area/length) and length for the
            # candidate filters, so a diagonal thin sliver (which has a
            # near-square bounding box) is not dropped before its true width is
            # ever measured.
            pts = _poly_vertices(poly)
            extent = _longest_extent(pts, bbox_diag)
            sliver_width = _estimate_width_mm(poly, short_dim_bbox, bbox_diag)
            if extent <= 0.0 or sliver_width <= 0.0:
                continue
            aspect_ratio = extent / sliver_width

            # sliver candidate filters (rotation invariant)
            if area < min_area_mm2:
                continue
            if aspect_ratio < min_aspect_ratio:
                continue
            if extent < min_long_dim_mm:
                continue
            if sliver_width < min_candidate_short_dim_mm:
                continue
            if sliver_width > max_short_dim_mm:
                continue
            if min_sliver is None or sliver_width < min_sliver:
                min_sliver = sliver_width
                cx = 0.5 * (b.min_x + b.max_x)
                cy = 0.5 * (b.min_y + b.max_y)
                worst_location = ViolationLocation(
                    layer=layer.logical_layer,
                    x_mm=cx,
                    y_mm=cy,
                    notes="Narrow copper region (sliver) based on polygon bounding box.",
                )

    if min_sliver is None:
        viol = Violation(
            severity="info",
            message="No elongated copper regions found that match sliver criteria.",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            severity=ctx.check_def.severity,
            status="pass",
            score=100.0,
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

    # status
    if min_sliver < absolute_min:
        status = "fail"
        severity = "error"
    elif min_sliver < recommended_min:
        status = "warning"
        severity = "warning"
    else:
        status = "pass"
        severity = ctx.check_def.severity or "error"

    # score
    if min_sliver >= recommended_min:
        score = 100.0
    elif min_sliver <= absolute_min:
        score = 0.0
    else:
        span = recommended_min - absolute_min
        score = max(0.0, min(100.0, 100.0 * (min_sliver - absolute_min) / span))

    margin_to_limit = float(min_sliver - absolute_min)

    violations: List[Violation] = []
    if status != "pass":
        msg = (
            f"Minimum copper sliver width {min_sliver:.3f} mm is below "
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
            "measured_value": float(min_sliver),
            "target": recommended_min,
            "limit_low": absolute_min,
            "limit_high": None,
            "margin_to_limit": margin_to_limit,
        },
        violations=violations,
    )
