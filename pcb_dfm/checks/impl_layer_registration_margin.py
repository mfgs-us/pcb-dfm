from __future__ import annotations

from math import floor
from typing import List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry import queries
from ..results import CheckResult, MetricResult, Violation, ViolationLocation
from .impl_min_annular_ring import (
    _collect_drills_from_excellon,
    _is_pad_like_polygon,
    _min_distance_to_polygon_edges,
    _point_in_polygon,
)


def _thresholds(ctx: CheckContext) -> Tuple[float, float]:
    """Registration-budget thresholds in mm (plumbed from the µm metric).

    ``recommended_min`` is the copper margin a fab wants around a hole to
    absorb expected layer-to-layer registration error; ``absolute_min`` is the
    floor below which misregistration is likely to break the ring out.
    """
    limits = ctx.check_def.limits or {}
    recommended_min = float(limits.get("recommended_min", 0.05))  # 50 µm
    absolute_min = float(limits.get("absolute_min", 0.025))       # 25 µm
    return recommended_min, absolute_min


def _copper_layer_count(ctx: CheckContext) -> Optional[int]:
    dd = getattr(ctx, "design_data", None)
    stackup = getattr(dd, "stackup", None) if dd else None
    if stackup is None:
        return None
    try:
        n = len(stackup.copper_layers())
    except Exception:
        return None
    return n or None


@register_check("layer_registration_margin")
def run_layer_registration_margin(ctx: CheckContext) -> CheckResult:
    """Copper margin available to absorb layer-to-layer registration error.

    Fabrication registration lets each layer shift by a small tolerance
    relative to the drilled holes. The copper that absorbs that shift is the
    annular ring around every plated hole, so we reuse the exact drill-edge to
    pad-edge geometry of ``min_annular_ring`` and read the worst-case ring
    through a *registration* lens: the pass/fail budget here is the stackup's
    registration tolerance (default 50 µm target / 25 µm floor), not the raw
    minimum-ring capability. A board can hold a nominal annular ring yet still
    fail this check if the ring is too thin to survive registration on a deep
    stack.
    """
    recommended_min, absolute_min = _thresholds(ctx)

    drills = _collect_drills_from_excellon(ctx)
    copper_layers = queries.get_copper_layers(ctx.geometry)
    if not drills or not copper_layers:
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="not_applicable",
            severity="info",
            score=None,
            metric=MetricResult.geometry_mm(None, target_mm=recommended_min, limit_low_mm=absolute_min),
            violations=[Violation(
                severity="info",
                message="No plated drills or copper geometry available to evaluate registration margin.",
                location=None,
            )],
        ).finalize()

    min_drill_dia = min(d.diameter_mm for d in drills)
    pad_candidates: List[tuple] = []
    for layer in copper_layers:
        for poly in layer.polygons:
            if _is_pad_like_polygon(poly, min_drill_dia, absolute_min):
                pad_candidates.append((poly, layer.logical_layer))

    if not pad_candidates:
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="not_applicable",
            severity="info",
            score=None,
            metric=MetricResult.geometry_mm(None, target_mm=recommended_min, limit_low_mm=absolute_min),
            violations=[Violation(
                severity="info",
                message="No pad-like copper features found around drills; registration margin not applicable.",
                location=None,
            )],
        ).finalize()

    # Spatial grid over pad candidates (mirrors min_annular_ring).
    cell = max(0.5, max(d.diameter_mm for d in drills))
    grid: dict = {}
    for idx, (poly, _name) in enumerate(pad_candidates):
        b = poly.bounds()
        for iy in range(int(floor(b.min_y / cell)), int(floor(b.max_y / cell)) + 1):
            for ix in range(int(floor(b.min_x / cell)), int(floor(b.max_x / cell)) + 1):
                grid.setdefault((ix, iy), []).append(idx)

    min_ring: Optional[float] = None
    worst_location: Optional[ViolationLocation] = None
    for hole in drills:
        r_drill = hole.diameter_mm * 0.5
        ci = int(floor(hole.x_mm / cell))
        cj = int(floor(hole.y_mm / cell))
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for idx in grid.get((ci + di, cj + dj), []):
                    poly, layer_name = pad_candidates[idx]
                    if not _point_in_polygon(hole.x_mm, hole.y_mm, poly.vertices):
                        continue
                    ring = _min_distance_to_polygon_edges(hole.x_mm, hole.y_mm, poly.vertices) - r_drill
                    if ring < 0.0:
                        ring = 0.0
                    if min_ring is None or ring < min_ring:
                        min_ring = ring
                        worst_location = ViolationLocation(
                            layer=layer_name,
                            x_mm=hole.x_mm,
                            y_mm=hole.y_mm,
                            notes="Thinnest annular ring available to absorb registration error.",
                        )

    if min_ring is None:
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="not_applicable",
            severity="info",
            score=None,
            metric=MetricResult.geometry_mm(None, target_mm=recommended_min, limit_low_mm=absolute_min),
            violations=[Violation(
                severity="info",
                message="No pad-drill combinations found to evaluate registration margin.",
                location=None,
            )],
        ).finalize()

    if min_ring < absolute_min:
        status = "fail"
    elif min_ring < recommended_min:
        status = "warning"
    else:
        status = "pass"

    if min_ring >= recommended_min:
        score = 100.0
    elif min_ring <= absolute_min:
        score = 0.0
    else:
        span = max(1e-9, recommended_min - absolute_min)
        score = max(0.0, min(100.0, 100.0 * (min_ring - absolute_min) / span))

    violations: List[Violation] = []
    if status != "pass":
        n_cu = _copper_layer_count(ctx)
        stack_note = f" on a {n_cu}-layer stack" if n_cu and n_cu > 2 else ""
        violations.append(Violation(
            severity=(ctx.check_def.severity or "warning") if status == "warning" else "error",
            message=(
                f"Thinnest annular ring is {min_ring * 1000:.0f} µm{stack_note}, below the "
                f"{recommended_min * 1000:.0f} µm registration budget "
                f"(floor {absolute_min * 1000:.0f} µm)."
            ),
            location=worst_location,
        ))

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",
        score=score,
        metric=MetricResult.geometry_mm(
            measured_mm=float(min_ring),
            target_mm=recommended_min,
            limit_low_mm=absolute_min,
        ),
        violations=violations,
    ).finalize()
