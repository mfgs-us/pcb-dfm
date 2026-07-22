"""
tombstoning_risk — thermal asymmetry on small two-pad passives.

A chip resistor/capacitor tombstones when its two terminations reach reflow at
different times: the pad tied to more copper (a plane or a wide pour) sinks heat
and melts its solder later, so surface tension on the already-molten side lifts
the part on end. The dominant, geometry-visible driver is a *thermal-mass
imbalance* between the two pads.

Using the component placement from a design source (KiCad footprints -> #6's
DesignData.components) plus the copper geometry, this estimates, for each small
two-pad passive, the local copper coverage under each pad and reports the
imbalance:

    imbalance% = 100 · |f1 − f2| / (f1 + f2)

where f is the fraction of a small disk under the pad that is covered by copper
(a plane gives ~1, a thin trace ~little). Explicitly heuristic: pad positions
are estimated from the footprint size code, not read from a library. Metric:
worst imbalance % (minimize), target 10 / limit 25.
"""

from __future__ import annotations

import math
import re
from typing import List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.net_map import _canon_layer
from ..geometry.polygon_index import PolygonIndex
from ..geometry.primitives import Bounds
from ..results import CheckResult, MetricResult, Violation, ViolationLocation
from .impl_min_annular_ring import _point_in_polygon

Point = Tuple[float, float]

# Distance between the two pad centres (mm) by imperial size code -- rough,
# library-independent nominal values (that is why the check is heuristic).
_PAD_SPACING_MM = {
    "0201": 0.65, "0402": 0.90, "0603": 1.55, "0805": 2.00,
    "1206": 3.00, "1210": 3.00, "2010": 4.90, "2512": 6.00,
}
_PASSIVE_RE = re.compile(r"(?:^|:)[RCL]_(\d{4})(?:_|\b)", re.IGNORECASE)


def _thresholds(ctx: CheckContext) -> Tuple[float, float]:
    m = ctx.check_def.metric or {}
    t = m.get("target") or {}
    limits = m.get("limits") or {}
    target = float(t.get("max", 10.0)) if isinstance(t, dict) else 10.0
    limit = float(limits.get("max", 25.0)) if isinstance(limits, dict) else 25.0
    return target, limit


def _na(ctx: CheckContext, target: float, limit: float, msg: str) -> CheckResult:
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="not_applicable",
        severity="info",
        score=None,
        metric=MetricResult.ratio_percent(None, target_pct=target, limit_high_pct=limit),
        violations=[Violation(severity="info", message=msg, location=None)],
    ).finalize()


def _passive_spacing(footprint: Optional[str]) -> Optional[float]:
    if not footprint:
        return None
    m = _PASSIVE_RE.search(footprint)
    if not m:
        return None
    return _PAD_SPACING_MM.get(m.group(1))


def _pad_centers(cx: float, cy: float, rot_deg: float, spacing: float) -> Tuple[Point, Point]:
    half = 0.5 * spacing
    a = math.radians(rot_deg)
    dx, dy = half * math.cos(a), half * math.sin(a)
    return (cx + dx, cy + dy), (cx - dx, cy - dy)


def _local_copper_fraction(px: float, py: float, radius: float, polys, grid: int = 7) -> float:
    """Fraction of a disk of ``radius`` around (px, py) covered by ``polys``."""
    if not polys:
        return 0.0
    covered = 0
    total = 0
    r2 = radius * radius
    for i in range(grid):
        gx = px - radius + (2 * radius) * (i + 0.5) / grid
        for j in range(grid):
            gy = py - radius + (2 * radius) * (j + 0.5) / grid
            if (gx - px) ** 2 + (gy - py) ** 2 > r2:
                continue
            total += 1
            if any(_point_in_polygon(gx, gy, p.vertices) for p in polys):
                covered += 1
    return (covered / total) if total else 0.0


@register_check("tombstoning_risk")
def run_tombstoning_risk(ctx: CheckContext) -> CheckResult:
    target, limit = _thresholds(ctx)
    dd = ctx.design_data
    if dd is None or not getattr(dd, "components", None):
        return _na(ctx, target, limit,
                   "No component placement; tombstoning needs a design source with components "
                   "(e.g. a KiCad project).")

    raw = ctx.check_def.raw or {}
    radius = float(raw.get("pad_sample_radius_mm", 0.6))

    # Copper polygons per side, with a spatial index for local sampling.
    copper_by_side = {"top": [], "bottom": []}
    for lyr in ctx.geometry.get_layers_by_type("copper"):
        canon = _canon_layer(lyr.logical_layer)
        side = "top" if canon == "top" else "bottom" if canon == "bottom" else None
        if side:
            copper_by_side[side].extend(p for p in lyr.polygons if len(p.vertices) >= 3)
    index_by_side = {
        side: (PolygonIndex.from_polygons(polys) if polys else None)
        for side, polys in copper_by_side.items()
    }

    passives = 0
    evaluated = 0
    worst = 0.0
    worst_ref: Optional[str] = None
    worst_loc: Optional[Point] = None
    violations: List[Violation] = []

    for comp in dd.components:
        spacing = _passive_spacing(comp.footprint)
        if spacing is None or comp.x_mm is None or comp.y_mm is None:
            continue
        passives += 1
        side = comp.side if comp.side in ("top", "bottom") else "top"
        polys = copper_by_side.get(side) or []
        index = index_by_side.get(side)
        if not polys or index is None:
            continue

        p1, p2 = _pad_centers(comp.x_mm, comp.y_mm, comp.rotation_deg, spacing)
        fracs = []
        for px, py in (p1, p2):
            disk = Bounds(px - radius, py - radius, px + radius, py + radius)
            cand = [polys[int(i)] for i in index.query_bbox(disk)]
            fracs.append(_local_copper_fraction(px, py, radius, cand))
        f1, f2 = fracs
        if (f1 + f2) <= 0.05:
            continue  # neither pad is on meaningful copper -> can't assess
        evaluated += 1

        imbalance = 100.0 * abs(f1 - f2) / (f1 + f2)
        if imbalance > worst:
            worst = imbalance
            worst_ref = comp.ref
            worst_loc = (comp.x_mm, comp.y_mm)
        if imbalance > target:
            violations.append(Violation(
                severity=ctx.check_def.severity or "warning",
                message=(
                    f"{comp.ref}: {imbalance:.0f}% copper thermal-mass imbalance between pads "
                    f"(target ≤ {target:.0f}%, limit ≤ {limit:.0f}%) — tombstoning risk. "
                    f"Heuristic estimate from placement."
                ),
                location=ViolationLocation(
                    layer=side, x_mm=comp.x_mm, y_mm=comp.y_mm,
                    notes="Asymmetric copper connection on a two-pad passive.",
                ),
            ))

    if passives == 0:
        return _na(ctx, target, limit, "No two-pad passive footprints found to evaluate.")
    if evaluated == 0:
        return _na(ctx, target, limit,
                   "No passive pads sit over copper that could be sampled for thermal balance.")

    if worst > limit:
        status = "fail"
    elif worst > target:
        status = "warning"
    else:
        status = "pass"

    if worst <= target:
        score = 100.0
    elif worst >= limit:
        score = 0.0
    else:
        span = max(1e-9, limit - target)
        score = max(0.0, min(100.0, 100.0 * (limit - worst) / span))

    _ = (worst_ref, worst_loc)  # captured in per-component violations above
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",
        score=score,
        metric=MetricResult.ratio_percent(
            measured_pct=float(worst), target_pct=target, limit_high_pct=limit,
        ),
        violations=violations,
    ).finalize()
