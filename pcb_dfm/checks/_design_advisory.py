"""Shared helpers for the design_advisory checks.

These checks are ADVISORY layout-quality screens, not fab hard-rejects. They read
only the artwork (no schematic), so every helper here is deliberately
conservative: when the geometry is ambiguous we would rather stay silent than
flag a false positive, because the whole value of the tier is that its findings
are worth a designer's attention.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from ..engine.context import CheckContext
from ..geometry.gerber_backend import outline_contours_mm
from ..geometry.primitives import Point2D
from ..results import CheckResult, MetricResult, Violation, ViolationLocation
from .impl_min_annular_ring import _point_in_polygon


def advisory(ctx: CheckContext, flagged: bool, metric: MetricResult,
             message: str, location: Optional[ViolationLocation] = None) -> CheckResult:
    """Standard design_advisory outcome: warning when flagged, pass when clean.

    Design-advisory checks NEVER hard-fail -- they surface things a reviewer
    should look at, not fab rejects. Score is 60 (warning) or 100 (pass).
    """
    status = "warning" if flagged else "pass"
    sev = "warning" if flagged else "info"
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity=sev,
        score=60.0 if flagged else 100.0,
        metric=metric,
        violations=[Violation(
            severity=sev, message=message,
            location=location if flagged else None,
        )],
    ).finalize()


def na(ctx: CheckContext, message: str, units: str = "count") -> CheckResult:
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="not_applicable",
        severity="info",
        score=None,
        metric=MetricResult(kind="count", units=units, measured_value=None),
        violations=[Violation(severity="info", message=message, location=None)],
    ).finalize()


def count_metric(count: int, target_max: float = 0.0) -> MetricResult:
    return MetricResult(kind="count", units="count",
                        measured_value=float(count), target=float(target_max))


def dist_metric(value: Optional[float], target_max: float) -> MetricResult:
    return MetricResult(kind="distance", units="mm",
                        measured_value=(float(value) if value is not None else None),
                        target=float(target_max))


def _poly_area(verts: List[Tuple[float, float]]) -> float:
    s = 0.0
    n = len(verts)
    for i in range(n):
        x1, y1 = verts[i]
        x2, y2 = verts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


def board_contour_verts(ctx: CheckContext) -> Optional[List[Tuple[float, float]]]:
    """The largest closed board-outline contour as a list of (x, y), or None.

    Uses the same contour assembly as copper_to_edge_distance (#18): stray
    dimension lines / plot marks are open chains and dropped; the largest closed
    contour is the board boundary.
    """
    ingest = getattr(ctx, "ingest", None)
    outline_files = [
        f for f in (getattr(ingest, "files", None) or [])
        if getattr(f, "layer_type", None) == "outline"
    ]
    best: Optional[List[Tuple[float, float]]] = None
    best_area = -1.0
    for f in outline_files:
        for verts in outline_contours_mm(f.path):
            if len(verts) < 3:
                continue
            a = _poly_area(verts)
            if a > best_area:
                best_area = a
                best = verts
    return best


def point_inside(verts: List[Tuple[float, float]], x: float, y: float) -> bool:
    """Point-in-polygon against a vertex list."""
    return _point_in_polygon(x, y, [Point2D(x=vx, y=vy) for (vx, vy) in verts])


def poly_area_mm2(poly) -> float:
    if hasattr(poly, "area_mm2"):
        return float(poly.area_mm2)
    if hasattr(poly, "area"):
        try:
            return float(poly.area())
        except TypeError:
            return float(poly.area)
    b = poly.bounds()
    return max(0.0, (b.max_x - b.min_x) * (b.max_y - b.min_y))


def is_pad_like(poly, min_area_mm2: float = 0.02, max_area_mm2: float = 25.0,
                max_aspect: float = 15.0) -> bool:
    """A copper polygon that plausibly is a component pad (not a pour, plane, or
    board-scale region). Deliberately generous on aspect (real pads include long
    connector fingers) but bounded on area so a ground pour never counts."""
    area = poly_area_mm2(poly)
    if area < min_area_mm2 or area > max_area_mm2:
        return False
    b = poly.bounds()
    w = max(0.0, b.max_x - b.min_x)
    h = max(0.0, b.max_y - b.min_y)
    if w <= 0.0 or h <= 0.0:
        return False
    short, long_ = min(w, h), max(w, h)
    return (long_ / short if short > 0.0 else 1.0) <= max_aspect


def bbox_center(b) -> Tuple[float, float]:
    return 0.5 * (b.min_x + b.max_x), 0.5 * (b.min_y + b.max_y)
