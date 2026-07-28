"""Unrouted net -- a multi-pin net with no copper at all.

A net with pins on two or more components but *no routed copper* (no tracks, no
pour) is unrouted -- ratsnest remaining. This is the unambiguous case; judging
*partial* connectivity from segments would require a full flood (pours, zone
stitching, exact via alignment) that the segment model doesn't capture, so it is
deliberately out of scope here to stay false-positive-free. Power/ground nets
(which connect through planes) are skipped.
"""

from __future__ import annotations

from typing import List

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult
from ._design_advisory import advisory, count_metric, na
from ._design_review import resolve_design


@register_check("unrouted_or_partial_net")
def run_unrouted_or_partial_net(ctx: CheckContext) -> CheckResult:
    dd = ctx.design_data
    if dd is None or not dd.nets:
        return na(ctx, "No nets; not applicable.")
    rd = resolve_design(dd)
    if rd is None:
        return na(ctx, "No netlist + BOM to resolve net pins; not applicable.")
    if not any(p.width_mm for c in dd.components for p in c.pads):
        return na(ctx, "No pad geometry; routing-completeness review not applicable.")

    unrouted: List[str] = []
    reviewed = 0
    for name, net in dd.nets.items():
        if len(rd.comps_on(name)) < 2:
            continue
        # Power/ground nets connect through pours/planes we don't model.
        if rd.net_func.get(name) in ("power", "ground"):
            continue
        if len(rd.pads_on(name)) < 2:
            continue
        reviewed += 1
        if not net.route_segments() and not net.fill_regions and not net.vias:
            unrouted.append(name)

    if reviewed == 0:
        return na(ctx, "No multi-component nets with resolvable pads; not applicable.")
    if unrouted:
        return advisory(
            ctx, True, count_metric(len(unrouted)),
            f"{len(unrouted)} multi-pin net(s) have no copper routing (ratsnest "
            f"remaining): {', '.join(sorted(unrouted)[:8])}.")
    return advisory(ctx, False, count_metric(0), "No unrouted multi-pin nets.")
