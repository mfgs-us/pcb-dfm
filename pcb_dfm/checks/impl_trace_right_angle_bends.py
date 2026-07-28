"""Right-angle bends on high-speed traces.

Restricted to shape-sensitive nets (declared diff-pairs, controlled-impedance,
or high-speed by name): a 90-deg bend is a real impedance discontinuity there.
On ordinary signals a right-angle is harmless, so flagging every corner would be
folklore -- hence the net gating and the not_applicable when no such nets exist.
"""

from __future__ import annotations

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, count_metric, na
from ._trace_geom import bend_angles, segments_by_layer, si_relevant_nets


@register_check("trace_right_angle_bends")
def run_trace_right_angle_bends(ctx: CheckContext) -> CheckResult:
    dd = ctx.design_data
    if dd is None or not dd.nets:
        return na(ctx, "No routed nets; not applicable.")
    if not any(n.has_geometry() for n in dd.nets.values()):
        return na(ctx, "No routed-segment geometry (need design data); not applicable.")
    si = si_relevant_nets(dd)
    if not si:
        return na(ctx, "No high-speed / diff-pair / controlled-impedance nets to check bend geometry; not applicable.")
    angle_max = float((ctx.check_def.raw.get("params", {}) or {}).get("angle_max_deg", 95.0))

    flagged = []  # (net, x, y, angle)
    for name in sorted(si):
        net = dd.nets[name]
        if not net.has_geometry():
            continue
        for _layer, segs in segments_by_layer(net).items():
            for (vx, vy), ang in bend_angles(segs):
                # Ignore near-straight joints and degenerate tiny stubs.
                if ang <= angle_max and ang > 5.0:
                    flagged.append((name, vx, vy, ang))
    # De-dup nets for the message; count individual bends for the metric.
    if flagged:
        nets = sorted({f[0] for f in flagged})
        loc = ViolationLocation(layer=None, x_mm=flagged[0][1], y_mm=flagged[0][2],
                                notes=f"{flagged[0][3]:.0f} deg bend on {flagged[0][0]}.")
        msg = (f"{len(flagged)} right-angle-or-sharper bend(s) on high-speed net(s) "
               f"{', '.join(nets[:6])}; use 45-deg chamfers or arcs.")
        return advisory(ctx, True, count_metric(len(flagged)), msg, location=loc)
    return advisory(ctx, False, count_metric(0),
                    f"No right-angle bends on the {len(si)} shape-sensitive net(s).")
