"""LED series resistor -- an LED directly across a rail with no current limit."""

from __future__ import annotations

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult
from ._design_advisory import advisory, count_metric, na
from ._design_review import resolve_design


@register_check("led_series_resistor")
def run_led_series_resistor(ctx: CheckContext) -> CheckResult:
    r = resolve_design(ctx.design_data)
    if r is None:
        return na(ctx, "No netlist + BOM to resolve LEDs; not applicable.")
    leds = r.refs_of_class("led")
    if not leds:
        return na(ctx, "No LEDs in the design; not applicable.")

    bad = []
    for d in leds:
        nets = r.nets_of(d)
        if len(nets) != 2:
            continue
        if any(r.has_class_on(n, "resistor") for n in nets):
            continue  # a series/ballast resistor is present
        if any(r.has_class_on(n, "ic") or r.has_class_on(n, "transistor") for n in nets):
            continue  # driven by a chip/transistor -> limiting may be elsewhere
        funcs = {r.net_func.get(n) for n in nets}
        if "power" in funcs and "ground" in funcs:
            bad.append(d)  # sits straight across a rail with no limit
    bad.sort()
    flagged = bool(bad)
    msg = (f"LED(s) across a power rail with no series resistor: {', '.join(bad)} "
           f"-> over-current.") if flagged else "No unlimited LEDs across a rail."
    return advisory(ctx, flagged, count_metric(len(bad)), msg)
