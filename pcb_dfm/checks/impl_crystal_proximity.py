"""Crystal proximity -- a crystal should sit close to its host IC."""

from __future__ import annotations

from math import hypot

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, count_metric, na
from ._design_review import resolve_design


@register_check("crystal_proximity")
def run_crystal_proximity(ctx: CheckContext) -> CheckResult:
    r = resolve_design(ctx.design_data)
    if r is None:
        return na(ctx, "No netlist + BOM to resolve crystals; not applicable.")
    crystals = r.refs_of_class("crystal")
    if not crystals:
        return na(ctx, "No crystal/resonator in the design; not applicable.")
    max_d = float((ctx.check_def.raw.get("params", {}) or {}).get("max_distance_mm", 10.0))

    far = []  # (ref, distance, x, y)
    reviewed = 0
    for y in crystals:
        yc = r.comp_by_ref[y]
        if yc.x_mm is None:
            continue
        # Host IC = an IC sharing this crystal's oscillator (signal) net.
        hosts = {ref for n in r.nets_of(y) if r.net_func.get(n) == "signal"
                 for ref in r.comps_on(n) if r.part_class.get(ref) == "ic"}
        best = None
        for ic in hosts:
            c = r.comp_by_ref.get(ic)
            if c is None or c.x_mm is None:
                continue
            d = hypot(yc.x_mm - c.x_mm, yc.y_mm - c.y_mm)
            best = d if best is None else min(best, d)
        if best is None:
            continue  # no identifiable host -> can't judge proximity
        reviewed += 1
        if best > max_d:
            far.append((y, best, yc.x_mm, yc.y_mm))
    if reviewed == 0:
        return na(ctx, "No crystal could be paired with a host IC; not applicable.")
    far.sort()
    flagged = bool(far)
    if flagged:
        loc = ViolationLocation(layer=None, x_mm=far[0][2], y_mm=far[0][3],
                                notes=f"Crystal {far[0][0]} far from its IC.")
        msg = ("Crystal(s) far from their host IC: "
               + ", ".join(f"{ref} ({d:.1f} mm)" for ref, d, _x, _y in far[:6])
               + f" (> {max_d:.0f} mm).")
        return advisory(ctx, True, count_metric(len(far)), msg, location=loc)
    return advisory(ctx, False, count_metric(0), "Crystals are close to their host IC.")
