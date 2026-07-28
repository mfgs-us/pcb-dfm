"""Orphan / redundant via.

Orphan: a *signal* via with no trace endpoint at it -- it connects nothing.
(Power/ground stitching vias legitimately connect pour-to-pour with no trace, so
they are excluded.) Redundant: two vias of a net stacked at one location.
"""

from __future__ import annotations

from math import hypot
from typing import List, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..ingest.design_intel import classify_net
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, count_metric, na
from ._design_review import resolve_design
from ._trace_geom import coincident


@register_check("orphan_or_redundant_via")
def run_orphan_or_redundant_via(ctx: CheckContext) -> CheckResult:
    dd = ctx.design_data
    if dd is None or not dd.nets:
        return na(ctx, "No nets; not applicable.")
    if not any(n.vias for n in dd.nets.values()):
        return na(ctx, "No via data on nets (need design data); not applicable.")
    rd = resolve_design(dd)
    params = ctx.check_def.raw.get("params", {}) or {}
    coincide = float(params.get("coincide_mm", 0.1))
    tol = float(params.get("tol_mm", 0.3))

    orphans: List[Tuple[str, float, float]] = []
    redundant: List[Tuple[str, float, float]] = []
    for name, net in dd.nets.items():
        if not net.vias:
            continue
        # Redundant: any two of this net's vias stacked together.
        vs = [(v.x_mm, v.y_mm) for v in net.vias]
        for i in range(len(vs)):
            for j in range(i + 1, len(vs)):
                if hypot(vs[i][0] - vs[j][0], vs[i][1] - vs[j][1]) <= coincide:
                    redundant.append((name, vs[i][0], vs[i][1]))
                    break
        # Orphan: only signal vias (pours connect ground/power stitching vias).
        if classify_net(name, net.net_class) != "signal":
            continue
        endpoints = [pt for (seg, _l, _w) in net.route_segments() for pt in seg]
        pads = rd.pad_points_on(name) if rd is not None else []
        for (vx, vy) in vs:
            if not coincident((vx, vy), endpoints, tol) and not coincident((vx, vy), pads, tol):
                orphans.append((name, vx, vy))

    total = len(orphans) + len(redundant)
    if total:
        first = (orphans or redundant)[0]
        loc = ViolationLocation(layer="Via", x_mm=first[1], y_mm=first[2],
                                notes=f"Orphan/redundant via on {first[0]}.")
        bits = []
        if orphans:
            bits.append(f"{len(orphans)} orphan signal via(s) with no trace")
        if redundant:
            bits.append(f"{len(redundant)} redundant stacked via(s)")
        return advisory(ctx, True, count_metric(total), "; ".join(bits) + ".", location=loc)
    return advisory(ctx, False, count_metric(0), "No orphan or redundant vias.")
