"""Trace necking / width discontinuity on power & controlled-impedance nets.

A net that narrows mid-route is a current bottleneck (power) or an impedance
discontinuity (controlled-impedance). Restricted to those net classes because a
signal net legitimately necks to enter a fine-pitch pad -- flagging that would be
noise. A short narrow run (pad entry) is excluded via the min-run length.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, Optional

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..ingest.design_intel import classify_net
from ..results import CheckResult
from ._design_advisory import advisory, count_metric, na
from ._trace_geom import seg_len


@register_check("trace_necking")
def run_trace_necking(ctx: CheckContext) -> CheckResult:
    dd = ctx.design_data
    if dd is None or not dd.nets:
        return na(ctx, "No routed nets; not applicable.")
    if not any(n.has_geometry() for n in dd.nets.values()):
        return na(ctx, "No routed-segment geometry (need design data); not applicable.")
    params = ctx.check_def.raw.get("params", {}) or {}
    neck_frac = float(params.get("neck_fraction", 0.5))
    min_run = float(params.get("min_run_mm", 1.0))

    # Only nets where sustained width matters: power/ground and impedance-spec'd.
    imp_nets = {s.name for s in dd.controlled_impedance}
    candidates = [n for name, n in dd.nets.items()
                  if n.has_geometry()
                  and (classify_net(name, n.net_class) in ("power", "ground")
                       or name in imp_nets)]
    if not candidates:
        return na(ctx, "No power/ground or controlled-impedance routed nets; not applicable.")

    flagged = []  # (net, max_w, min_w, narrow_len)
    for net in candidates:
        # Total routed length at each width.
        by_width: Dict[Optional[float], float] = defaultdict(float)
        for (seg, _layer, width) in net.route_segments():
            if width and width > 0:
                by_width[width] += seg_len(seg)
        widths = [w for w in by_width if w]
        if len(widths) < 2:
            continue
        wmax, wmin = max(widths), min(widths)
        if wmin >= neck_frac * wmax:
            continue  # not a material neck-down
        narrow_len = sum(length for w, length in by_width.items()
                         if w and w < neck_frac * wmax)
        if narrow_len < min_run:
            continue  # a short stub (pad entry), not a sustained bottleneck
        flagged.append((net.name, wmax, wmin, narrow_len))
    flagged.sort(key=lambda t: t[2] / t[1])  # worst neck ratio first
    if flagged:
        msg = ("Net(s) necking down mid-route: "
               + ", ".join(f"{n} ({wmax:.2f}->{wmin:.2f} mm over {ln:.1f} mm)"
                           for n, wmax, wmin, ln in flagged[:6])
               + " -> current/impedance bottleneck.")
        return advisory(ctx, True, count_metric(len(flagged)), msg)
    return advisory(ctx, False, count_metric(0), "No necked power/impedance nets.")
