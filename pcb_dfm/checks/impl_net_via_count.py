"""Excessive layer changes (vias) per signal net.

A signal net with many vias adds inductance and via stubs and points at
congested routing. Signal nets only -- power/ground legitimately use many
stitching vias -- and a conservative threshold, so it flags outliers, not
ordinary multi-layer routing.
"""

from __future__ import annotations

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..ingest.design_intel import classify_net
from ..results import CheckResult
from ._design_advisory import advisory, count_metric, na


@register_check("net_via_count")
def run_net_via_count(ctx: CheckContext) -> CheckResult:
    dd = ctx.design_data
    if dd is None or not dd.nets:
        return na(ctx, "No nets; not applicable.")
    if not any(n.vias for n in dd.nets.values()):
        return na(ctx, "No via/layer-change data on nets (need design data); not applicable.")
    max_vias = int((ctx.check_def.raw.get("params", {}) or {}).get("max_vias", 8))

    flagged = []  # (net, via_count)
    for name, net in dd.nets.items():
        if classify_net(name, net.net_class) != "signal":
            continue
        if len(net.vias) > max_vias:
            flagged.append((name, len(net.vias)))
    flagged.sort(key=lambda t: -t[1])
    if flagged:
        msg = (f"Signal net(s) with more than {max_vias} layer changes: "
               + ", ".join(f"{n} ({v} vias)" for n, v in flagged[:6])
               + " -> via inductance/stubs; review routing.")
        return advisory(ctx, True, count_metric(len(flagged)), msg)
    return advisory(ctx, False, count_metric(0),
                    f"No signal net exceeds {max_vias} layer changes.")
