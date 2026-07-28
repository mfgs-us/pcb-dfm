"""Acute route angle -- an acute centreline bend traps etchant (acid trap).

Route-level complement to ``acid_trap_angle`` (which works on copper polygon
corners): this reads the routed centreline, so it attributes the acute angle to a
net and location. Any net -- acute angles are an etch concern regardless of speed.
"""

from __future__ import annotations

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, count_metric, na
from ._trace_geom import bend_angles, segments_by_layer


@register_check("acute_trace_angle")
def run_acute_trace_angle(ctx: CheckContext) -> CheckResult:
    dd = ctx.design_data
    if dd is None or not dd.nets:
        return na(ctx, "No routed nets; not applicable.")
    if not any(n.has_geometry() for n in dd.nets.values()):
        return na(ctx, "No routed-segment geometry (need design data); not applicable.")
    angle_min = float((ctx.check_def.raw.get("params", {}) or {}).get("angle_min_deg", 90.0))

    flagged = []  # (net, x, y, angle)
    for name, net in dd.nets.items():
        if not net.has_geometry():
            continue
        for _layer, segs in segments_by_layer(net).items():
            for (vx, vy), ang in bend_angles(segs):
                if 5.0 < ang < angle_min:   # acute, ignore degenerate spikes
                    flagged.append((name, vx, vy, ang))
    flagged.sort(key=lambda t: t[3])
    if flagged:
        nets = sorted({f[0] for f in flagged})
        loc = ViolationLocation(layer=None, x_mm=flagged[0][1], y_mm=flagged[0][2],
                                notes=f"{flagged[0][3]:.0f} deg acute bend on {flagged[0][0]}.")
        msg = (f"{len(flagged)} acute (<{angle_min:.0f} deg) route bend(s) on "
               f"{', '.join(nets[:6])} -> acid-trap / etch risk.")
        return advisory(ctx, True, count_metric(len(flagged)), msg, location=loc)
    return advisory(ctx, False, count_metric(0), "No acute route bends.")
