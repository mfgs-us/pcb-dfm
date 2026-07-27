"""IC ground/power connection -- an IC tied to no ground (or no power) rail."""

from __future__ import annotations

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult
from ._design_advisory import advisory, count_metric, na
from ._design_review import resolve_design


@register_check("unpowered_ic")
def run_unpowered_ic(ctx: CheckContext) -> CheckResult:
    r = resolve_design(ctx.design_data)
    if r is None:
        return na(ctx, "No netlist + BOM to resolve IC connections; not applicable.")
    grounds = r.ground_nets()
    if not grounds:
        return na(ctx, "No ground net identified; cannot review IC ground connection.")
    powers = r.power_nets()
    min_pins = int((ctx.check_def.raw.get("params", {}) or {}).get("min_pins", 8))

    no_gnd, no_pwr = [], []
    for ref, cls in r.part_class.items():
        if cls != "ic":
            continue
        comp = r.comp_by_ref[ref]
        npads = len(comp.pads)
        # Only large digital ICs, where GND naming is reliable. Small analog/power
        # parts (SOT-23 &c.) often reference a non-GND net (e.g. BAT_NEG) and would
        # false-positive.
        if npads < min_pins:
            continue
        # Require every pad resolved to a net -- an unmatched pad means our view is
        # incomplete, so we must not claim a pin is missing.
        if any((ref, p.name) not in r.idx.pad_net for p in comp.pads):
            continue
        nets = r.nets_of(ref)
        if not (nets & grounds):
            no_gnd.append(ref)
        elif powers and not (nets & powers):
            no_pwr.append(ref)
    no_gnd.sort()
    no_pwr.sort()
    flagged = bool(no_gnd or no_pwr)
    parts = []
    if no_gnd:
        parts.append(f"no ground connection: {', '.join(no_gnd)}")
    if no_pwr:
        parts.append(f"no power connection: {', '.join(no_pwr)}")
    msg = ("IC(s) with " + "; ".join(parts) + " -- verify.") if flagged \
        else "All multi-pin ICs reach a ground (and power) rail."
    return advisory(ctx, flagged, count_metric(len(no_gnd) + len(no_pwr)), msg)
