"""Exposed-pad thermal vias.

A component's exposed/thermal pad (the big centre pad under a QFN/DFN/power part)
should have thermal vias dropping heat into an inner/other-side plane. This finds
the exposed pad -- a pad markedly larger than the part's signal pads -- and counts
the vias landing on it. Needs pad geometry (to size the pad) and via locations.
"""

from __future__ import annotations

from statistics import median
from typing import List, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..ingest.design_intel import classify_component
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, count_metric, na


@register_check("thermal_pad_via_count")
def run_thermal_pad_via_count(ctx: CheckContext) -> CheckResult:
    dd = ctx.design_data
    if dd is None or not dd.components:
        return na(ctx, "No components; not applicable.")
    if not any(p.width_mm for c in dd.components for p in c.pads):
        return na(ctx, "No pad geometry; thermal-pad review not applicable.")
    params = ctx.check_def.raw.get("params", {}) or {}
    area_ratio = float(params.get("area_ratio", 4.0))
    min_area = float(params.get("min_area_mm2", 4.0))

    all_vias: List[Tuple[float, float]] = [
        (v.x_mm, v.y_mm) for net in dd.nets.values() for v in net.vias]

    exposed = 0
    starved: List[Tuple[str, float, float]] = []
    for c in dd.components:
        # Thermal/exposed pads live under ICs. A large pad on a connector or a
        # mounting part is mechanical, not a heat-sink pad.
        if classify_component(c)[0] != "ic":
            continue
        areas = [(p, p.area_mm2()) for p in c.pads if p.area_mm2() is not None]
        if len(areas) < 2:
            continue
        vals = [a for _p, a in areas]
        med = median(vals)
        if med <= 0:
            continue
        for pad, a in areas:
            # An exposed pad is much larger than the part's signal pads.
            if a < min_area or a < area_ratio * med:
                continue
            exposed += 1
            vias_in = sum(1 for (vx, vy) in all_vias if pad.contains(vx, vy))
            if vias_in == 0:
                starved.append((c.ref, pad.x_mm, pad.y_mm))
    if exposed == 0:
        return na(ctx, "No exposed/thermal pads detected; not applicable.")
    starved.sort()
    flagged = bool(starved)
    if flagged:
        loc = ViolationLocation(layer=None, x_mm=starved[0][1], y_mm=starved[0][2],
                                notes=f"Exposed pad on {starved[0][0]} has no thermal via.")
        msg = (f"Exposed/thermal pad(s) with no thermal via: "
               f"{', '.join(r for r, _x, _y in starved[:8])} -> poor heat-sinking.")
        return advisory(ctx, True, count_metric(len(starved)), msg, location=loc)
    return advisory(ctx, False, count_metric(0),
                    f"All {exposed} exposed pad(s) have thermal vias.")
