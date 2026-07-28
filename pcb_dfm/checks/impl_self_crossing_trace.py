"""Self-crossing trace -- a signal net whose route crosses itself on one layer."""

from __future__ import annotations

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..ingest.design_intel import classify_net
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, count_metric, na
from ._trace_geom import seg_len, segments_by_layer, segments_cross

_MIN_SEG_MM = 0.15  # ignore tiny chord/teardrop segments (crossings there are noise)


@register_check("self_crossing_trace")
def run_self_crossing_trace(ctx: CheckContext) -> CheckResult:
    dd = ctx.design_data
    if dd is None or not dd.nets:
        return na(ctx, "No routed nets; not applicable.")
    signal = [(name, n) for name, n in dd.nets.items()
              if n.has_geometry() and classify_net(name, n.net_class) == "signal"]
    if not signal:
        return na(ctx, "No routed signal nets; not applicable.")

    flagged = []  # (net, x, y)
    for name, net in signal:
        for _layer, segs in segments_by_layer(net).items():
            big = [s for s in segs if seg_len(s[0]) >= _MIN_SEG_MM]
            n = len(big)
            for i in range(n):
                for j in range(i + 1, n):
                    if segments_cross(big[i][0], big[j][0]):
                        s1 = big[i][0]
                        flagged.append((name, 0.5 * (s1[0][0] + s1[1][0]),
                                        0.5 * (s1[0][1] + s1[1][1])))
                        break
                else:
                    continue
                break
    flagged.sort()
    if flagged:
        loc = ViolationLocation(layer=None, x_mm=flagged[0][1], y_mm=flagged[0][2],
                                notes=f"{flagged[0][0]} crosses itself.")
        msg = (f"{len(flagged)} signal net(s) whose route crosses itself: "
               f"{', '.join(n for n, _x, _y in flagged[:6])}.")
        return advisory(ctx, True, count_metric(len(flagged)), msg, location=loc)
    return advisory(ctx, False, count_metric(0), "No self-crossing signal traces.")
