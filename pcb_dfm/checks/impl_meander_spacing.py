"""Meander / serpentine spacing on signal nets.

A length-tuning meander must keep adjacent legs >= ~3x trace width apart, or the
wiggle couples to itself -- losing effective delay and adding crosstalk. Detected
as near-parallel, same-layer legs *of the same net* that sit closer than the
spacing factor times their width.

Signal nets only: power/ground pours have dense parallel copper by nature (not a
meander), and excluding them also keeps this fast.
"""

from __future__ import annotations

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, count_metric, na
from ._trace_geom import (
    near_parallel,
    parallel_overlap_offset,
    seg_dir,
    seg_len,
    segments_by_layer,
    si_relevant_nets,
)

_MIN_LEG_MM = 0.3        # ignore tiny fragments
_MIN_OVERLAP_MM = 0.3    # legs must actually run alongside each other


@register_check("meander_spacing")
def run_meander_spacing(ctx: CheckContext) -> CheckResult:
    dd = ctx.design_data
    if dd is None or not dd.nets:
        return na(ctx, "No routed nets; not applicable.")
    # Meanders live on length-tuned nets (diff-pairs / high-speed). Restricting
    # here avoids flagging an ordinary signal that merely routes past itself.
    tuned = [(name, dd.nets[name]) for name in sorted(si_relevant_nets(dd))
             if dd.nets[name].has_geometry()]
    if not tuned:
        return na(ctx, "No high-speed / diff-pair nets that could be meandered; not applicable.")
    params = ctx.check_def.raw.get("params", {}) or {}
    factor = float(params.get("spacing_factor", 3.0))
    par_tol = float(params.get("parallel_tol_deg", 15.0))

    flagged = []  # (net, edge_gap, width, x, y)
    for name, net in tuned:
        for _layer, segs in segments_by_layer(net).items():
            legs = [(seg, w) for (seg, w) in segs if w and seg_len(seg) >= _MIN_LEG_MM]
            for i in range(len(legs)):
                s1, w1 = legs[i]
                d1 = seg_dir(s1)
                for j in range(i + 1, len(legs)):
                    s2, w2 = legs[j]
                    if not near_parallel(d1, seg_dir(s2), par_tol):
                        continue
                    po = parallel_overlap_offset(s1, s2)
                    if po is None:
                        continue
                    offset, overlap = po
                    w = min(w1, w2)
                    # Real, distinct legs running alongside each other -- exclude
                    # connected/collinear (offset ~ 0) and non-overlapping legs.
                    if overlap < _MIN_OVERLAP_MM or offset <= 1.05 * w:
                        continue
                    edge_gap = offset - w  # equal-width approximation
                    if edge_gap < factor * w:
                        mx = 0.25 * (s1[0][0] + s1[1][0] + s2[0][0] + s2[1][0])
                        my = 0.25 * (s1[0][1] + s1[1][1] + s2[0][1] + s2[1][1])
                        flagged.append((name, edge_gap, w, mx, my))
    # Worst (tightest) per net.
    best_per_net = {}
    for (name, gap, w, x, y) in flagged:
        if name not in best_per_net or gap < best_per_net[name][0]:
            best_per_net[name] = (gap, w, x, y)
    if best_per_net:
        items = sorted(best_per_net.items(), key=lambda kv: kv[1][0])
        n0, (g0, w0, x0, y0) = items[0]
        loc = ViolationLocation(layer=None, x_mm=x0, y_mm=y0,
                                notes=f"Meander legs {g0:.2f} mm apart on {n0}.")
        msg = ("Meander/serpentine legs closer than "
               f"{factor:.0f}x width on: "
               + ", ".join(f"{n} ({g:.2f} mm gap, {w:.2f} mm trace)"
                           for n, (g, w, _x, _y) in items[:6])
               + " -> self-coupling.")
        return advisory(ctx, True, count_metric(len(best_per_net)), msg, location=loc)
    return advisory(ctx, False, count_metric(0), "No tightly-spaced meanders on signal nets.")
