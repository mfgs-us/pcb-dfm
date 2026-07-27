"""Crystal load capacitors -- a 2-pin crystal needs a load cap per oscillator pin."""

from __future__ import annotations

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult
from ._design_advisory import advisory, count_metric, na
from ._design_review import resolve_design


@register_check("crystal_load_caps")
def run_crystal_load_caps(ctx: CheckContext) -> CheckResult:
    r = resolve_design(ctx.design_data)
    if r is None:
        return na(ctx, "No netlist + BOM to resolve crystals; not applicable.")
    if not r.ground_nets():
        return na(ctx, "No ground net identified; cannot review load caps.")
    crystals = r.refs_of_class("crystal")
    if not crystals:
        return na(ctx, "No crystal/resonator in the design; not applicable.")

    missing = []
    for y in crystals:
        # Oscillator pins are the crystal's signal nets. A classic 2-pin crystal
        # has exactly two; a 4-pin oscillator (has power/ground pins) is skipped.
        osc = [n for n in r.nets_of(y) if r.net_func.get(n) == "signal"]
        if len(osc) != 2:
            continue
        if any(not r.cap_to_ground_on(n) for n in osc):
            missing.append(y)
    missing.sort()
    flagged = bool(missing)
    msg = (f"Crystal(s) missing a load capacitor to ground on an oscillator pin: "
           f"{', '.join(missing)}.") if flagged else "All 2-pin crystals have load caps."
    return advisory(ctx, flagged, count_metric(len(missing)), msg)
