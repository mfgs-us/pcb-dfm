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

    rails = grounds | powers
    floating = []
    for ref, cls in r.part_class.items():
        if cls != "ic":
            continue
        comp = r.comp_by_ref[ref]
        npads = len(comp.pads)
        # Only large digital ICs, where rail naming is reliable.
        if npads < min_pins:
            continue
        # Require every pad resolved to a net -- an unmatched pad means our view is
        # incomplete, so we must not claim a pin is missing.
        if any((ref, p.name) not in r.idx.pad_net for p in comp.pads):
            continue
        # Flag only a truly floating IC -- one that reaches NEITHER a power nor a
        # ground rail. Requiring a *ground*-named net false-positives on parts
        # whose reference is an oddly-named net (a battery monitor's BAT-, a
        # regulator's sense return, a tube's cathode).
        if not (r.nets_of(ref) & rails):
            floating.append(ref)
    floating.sort()
    flagged = bool(floating)
    msg = (f"IC(s) not connected to any power or ground rail: "
           f"{', '.join(floating)} -- verify.") if flagged \
        else "All multi-pin ICs reach a power/ground rail."
    return advisory(ctx, flagged, count_metric(len(floating)), msg)
