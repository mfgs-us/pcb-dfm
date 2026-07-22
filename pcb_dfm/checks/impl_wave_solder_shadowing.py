"""
wave_solder_shadowing — through-hole pins starved of solder by a taller neighbor.

In wave soldering the board slides over a standing wave of molten solder. A tall
component casts a "shadow" on its trailing side, so a through-hole part sitting
just downstream in that shadow can get cold/incomplete joints.

Estimated from component identity + placement (BOM + KiCad, #6) plus the drill
map: a component is treated as through-hole (wave-soldered) when drilled holes
sit under its placement, heights come from the BOM or a per-class nominal, and a
part is flagged when a *taller* part leads it along the board's travel direction,
close and laterally overlapping.

Heavily heuristic and clearly labeled: the **travel direction is a fab process
parameter we don't get from artwork**, so it's assumed (raw.wave_travel_axis,
default "+x") — set it to match the panel. ``not_applicable`` without components,
drills, or at least two through-hole parts. Metric: % of through-hole parts
shadowed (minimize), target 10 / limit 30.
"""

from __future__ import annotations

from math import hypot
from typing import List, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, MetricResult, Violation, ViolationLocation
from .impl_drill_to_drill_spacing import _collect_drills

Point = Tuple[float, float]

_AXIS = {"+x": (1.0, 0.0), "-x": (-1.0, 0.0), "+y": (0.0, 1.0), "-y": (0.0, -1.0)}

# Per-class nominal body height (mm) when the BOM carries none.
_NOMINAL_HEIGHT = {
    "connector": 8.0, "relay": 10.0, "electrolytic": 6.0, "switch": 6.0,
    "inductor": 4.0, "crystal": 3.5, "ic": 3.0, "transistor": 3.0, "fuse": 3.0,
    "ferrite": 2.0, "diode": 2.0, "led": 2.0, "capacitor": 1.5, "resistor": 1.0,
}


def _thresholds(ctx: CheckContext) -> Tuple[float, float]:
    m = ctx.check_def.metric or {}
    t = m.get("target") or {}
    limits = m.get("limits") or {}
    target = float(t.get("max", 10.0)) if isinstance(t, dict) else 10.0
    limit = float(limits.get("max", 30.0)) if isinstance(limits, dict) else 30.0
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


def _height(comp) -> float:
    if comp.height_mm is not None and comp.height_mm > 0:
        return float(comp.height_mm)
    return _NOMINAL_HEIGHT.get(comp.part_class or "", 3.0)


@register_check("wave_solder_shadowing")
def run_wave_solder_shadowing(ctx: CheckContext) -> CheckResult:
    target, limit = _thresholds(ctx)
    dd = ctx.design_data
    if dd is None or not getattr(dd, "components", None):
        return _na(ctx, target, limit,
                   "No component identity/placement; provide a design source and BOM.")

    raw = ctx.check_def.raw or {}
    axis = _AXIS.get(str(raw.get("wave_travel_axis", "+x")).lower(), (1.0, 0.0))
    tx, ty = axis
    body_r = float(raw.get("body_radius_mm", 3.0))        # nominal half-width for THT test + lateral overlap
    height_margin = float(raw.get("height_margin_mm", 2.0))
    shadow_factor = float(raw.get("shadow_length_factor", 2.0))
    max_shadow = float(raw.get("max_shadow_mm", 15.0))

    placed = [c for c in dd.components
              if c.placed and not c.dnp and c.x_mm is not None and c.y_mm is not None]
    if not placed:
        return _na(ctx, target, limit, "No populated placed components to evaluate.")

    # Through-hole = a part with through-hole pads (preferred, from footprint
    # pads) or, absent pad geometry, a part with a drilled hole under its body.
    any_pads = any(c.pads for c in placed)
    drills = [] if any_pads else _collect_drills(ctx)
    if not any_pads and not drills:
        return _na(ctx, target, limit,
                   "No pad geometry or drill map; through-hole parts cannot be identified.")

    tht = []
    for c in placed:
        if c.pads:
            if any(p.through_hole for p in c.pads):
                tht.append(c)
        elif any(hypot(d.x_mm - c.x_mm, d.y_mm - c.y_mm) <= body_r for d in drills):
            tht.append(c)
    if len(tht) < 2:
        return _na(ctx, target, limit,
                   "Fewer than two through-hole parts; wave-solder shadowing not applicable.")

    def t_of(c) -> float:  # coordinate along travel direction
        return c.x_mm * tx + c.y_mm * ty

    def u_of(c) -> float:  # lateral coordinate
        return -c.x_mm * ty + c.y_mm * tx

    shadowed: List[Tuple[object, object]] = []
    for a in tht:
        ha = _height(a)
        ta, ua = t_of(a), u_of(a)
        for b in placed:
            if b is a:
                continue
            hb = _height(b)
            if hb < ha + height_margin:
                continue  # b not meaningfully taller
            gap = t_of(b) - ta  # b leads a along travel -> a in b's trailing shadow
            if gap <= 0.0:
                continue
            if gap > min(max_shadow, shadow_factor * hb):
                continue
            if abs(u_of(b) - ua) > 2.0 * body_r:
                continue  # no lateral overlap
            shadowed.append((a, b))
            break

    total = len(tht)
    measured = 100.0 * len(shadowed) / total

    if measured > limit:
        status = "fail"
    elif measured > target:
        status = "warning"
    else:
        status = "pass"

    if measured <= target:
        score = 100.0
    elif measured >= limit:
        score = 0.0
    else:
        span = max(1e-9, limit - target)
        score = max(0.0, min(100.0, 100.0 * (limit - measured) / span))

    violations: List[Violation] = []
    for a, b in shadowed[:100]:
        violations.append(Violation(
            severity=ctx.check_def.severity or "info",
            message=(
                f"{a.ref} (through-hole) is wave-solder shadowed by taller {b.ref} "
                f"upstream along {raw.get('wave_travel_axis', '+x')} travel. "
                f"Heuristic — verify the panel travel direction."
            ),
            location=ViolationLocation(
                x_mm=a.x_mm, y_mm=a.y_mm, component=a.ref,
                notes="Through-hole part in a taller neighbor's wave-solder shadow.",
            ),
        ))

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",
        score=score,
        metric=MetricResult.ratio_percent(
            measured_pct=float(measured), target_pct=target, limit_high_pct=limit,
        ),
        violations=violations,
    ).finalize()
