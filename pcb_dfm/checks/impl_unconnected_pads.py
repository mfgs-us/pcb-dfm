"""Design advisory: a two-terminal part with one leg floating is non-functional.

Flagging *every* net-less pad is a false-positive machine on real boards -- a
module or connector legitimately has dozens of intentionally-unconnected pins
(unused GPIO, castellated duplicates, mechanical pads), and without a schematic
we cannot tell an unused pin from a forgotten one.

The one net-less case that IS unambiguous without schematic intent: a
**two-terminal passive** (R / C / L / D / LED / ferrite / fuse) with exactly two
pads, one of which is on a net and the other on none. A resistor with a floating
leg does nothing -- that is a real defect, not a design choice. Everything else
(ICs, connectors, fully-net-less mechanical parts, DNP) is deliberately left
alone. Needs a netlist + component pad geometry; otherwise not_applicable.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..ingest.design_intel import build_pad_net_index, classify_component
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, count_metric, na

# Two-terminal parts: both terminals must connect or the part is dead.
_TWO_TERMINAL = {"resistor", "capacitor", "inductor", "diode", "led",
                 "ferrite", "fuse"}


@register_check("unconnected_pads")
def run_unconnected_pads(ctx: CheckContext) -> CheckResult:
    dd = getattr(ctx, "design_data", None)
    if dd is None or not dd.nets or not dd.components:
        return na(ctx, "Needs a netlist and component placement to resolve pads to nets.")
    if not any(c.pads for c in dd.components):
        return na(ctx, "Components carry no pad geometry; cannot resolve pads to nets.")

    idx = build_pad_net_index(dd)

    flagged: List[Tuple[str, Optional[Tuple[float, float]]]] = []
    for comp in dd.components:
        if getattr(comp, "dnp", False):
            continue
        cls, _ = classify_component(comp)
        if cls not in _TWO_TERMINAL:
            continue
        pads = comp.pads
        if len(pads) != 2:
            continue  # only the clean two-terminal case is unambiguous
        netted = [p for p in pads if (comp.ref, p.name) in idx.pad_net]
        floating = [p for p in pads if (comp.ref, p.name) not in idx.pad_net]
        # Exactly one leg connected, one floating -> the part is dead.
        if len(netted) == 1 and len(floating) == 1:
            p = floating[0]
            flagged.append((comp.ref, (p.x_mm, p.y_mm)))

    count = len(flagged)
    if count == 0:
        return advisory(ctx, False, count_metric(0),
                        "No two-terminal part has a floating leg.")
    ref, loc = flagged[0]
    vloc = ViolationLocation(layer="Copper", x_mm=loc[0], y_mm=loc[1],
                             notes=f"{ref} has one terminal on no net.") if loc else None
    return advisory(
        ctx, True, count_metric(count),
        f"{count} two-terminal part(s) have one leg on a net and the other floating "
        f"(e.g. {ref}) -- the part is electrically dead. Connect the open terminal.",
        vloc,
    )
