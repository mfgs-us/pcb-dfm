"""Schematic / layout pin consistency.

A component's schematic pins should each have a matching footprint pad. A pin
with no pad is a wrong-footprint or pin-mapping error -- a silent bug only
findable by cross-checking the two sources. Now trustworthy thanks to the
no-connect ingest: an *intentionally* unused pin (marked no-connect) legitimately
has no pad, so only a **non-no-connect** schematic pin with no pad is flagged.

Also limited to numeric pin numbers -- a mechanical/shield pin ("MP", "SH") is
allowed to exist without a pad. Needs a schematic; not applicable without one.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Set

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult
from ._design_advisory import advisory, count_metric, na


@register_check("schematic_layout_consistency")
def run_schematic_layout_consistency(ctx: CheckContext) -> CheckResult:
    dd = ctx.design_data
    if dd is None or not dd.components:
        return na(ctx, "No components; not applicable.")
    if not dd.pin_types:
        return na(ctx, "No schematic pin data; schematic/layout consistency not applicable.")

    sch_pins: Dict[str, Set[str]] = defaultdict(set)
    for (ref, pin) in dd.pin_types:
        sch_pins[ref].add(pin)

    bad: List[str] = []
    reviewed = 0
    for c in dd.components:
        if c.ref not in sch_pins or not c.pads:
            continue  # not in the schematic, or not placed
        reviewed += 1
        lay = {p.name for p in c.pads}
        missing = [pin for pin in sch_pins[c.ref]
                   if pin not in lay
                   and pin.isdigit()                        # skip mechanical pins
                   and (c.ref, pin) not in dd.nc_pins]       # skip intentional NC
        if missing:
            bad.append(f"{c.ref} (pin(s) {', '.join(sorted(missing)[:6])})")
    if reviewed == 0:
        return na(ctx, "No components resolvable in both schematic and layout; not applicable.")
    bad.sort()
    flagged = bool(bad)
    msg = (f"{len(bad)} component(s) with a connected schematic pin that has no "
           f"footprint pad: {'; '.join(bad[:6])} -> wrong footprint / pin-mapping.") if flagged \
        else "Every connected schematic pin has a matching footprint pad."
    return advisory(ctx, flagged, count_metric(len(bad)), msg)
