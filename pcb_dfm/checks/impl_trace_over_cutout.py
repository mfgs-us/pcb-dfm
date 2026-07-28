"""Trace over board cutout -- a trace routed across an internal slot/void.

Copper over a cutout has no dielectric support and no reference plane under it.
Uses the internal board-outline contours (the cutouts, i.e. every closed contour
inside the boundary) against the routed segments. Needs both a routed-segment
model and an outline with cutouts, else not_applicable.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.gerber_backend import GERBONARA_AVAILABLE, outline_contours_mm
from ..geometry.primitives import Point2D
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, count_metric, na
from ._trace_geom import segments_cross
from .impl_min_annular_ring import _point_in_polygon


def _seg_hits_cutout(seg, cutout: List[Tuple[float, float]], verts) -> bool:
    a, b = seg
    if _point_in_polygon(a[0], a[1], verts) or _point_in_polygon(b[0], b[1], verts):
        return True
    mid = (0.5 * (a[0] + b[0]), 0.5 * (a[1] + b[1]))
    if _point_in_polygon(mid[0], mid[1], verts):
        return True
    n = len(cutout)
    for i in range(n):
        edge = (cutout[i], cutout[(i + 1) % n])
        if segments_cross(seg, edge):
            return True
    return False


@register_check("trace_over_cutout")
def run_trace_over_cutout(ctx: CheckContext) -> CheckResult:
    dd = ctx.design_data
    if dd is None or not dd.nets or not any(n.has_geometry() for n in dd.nets.values()):
        return na(ctx, "No routed-segment geometry (need design data); not applicable.")
    if not GERBONARA_AVAILABLE:
        return na(ctx, "Gerber parser unavailable; not applicable.")
    outline_file = next((f for f in ctx.ingest.files if f.layer_type == "outline"), None) \
        if ctx.ingest is not None else None
    if outline_file is None:
        return na(ctx, "No board outline layer; not applicable.")
    contours = outline_contours_mm(Path(outline_file.path))
    cutouts = contours[1:] if len(contours) > 1 else []
    if not cutouts:
        return na(ctx, "No internal board cutouts; not applicable.")
    cut_verts = [[Point2D(x, y) for (x, y) in c] for c in cutouts]

    hits: List[Tuple[str, float, float]] = []
    for name, net in dd.nets.items():
        if not net.has_geometry():
            continue
        for (seg, _layer, _w) in net.route_segments():
            for cut, verts in zip(cutouts, cut_verts):
                if _seg_hits_cutout(seg, cut, verts):
                    hits.append((name, 0.5 * (seg[0][0] + seg[1][0]),
                                 0.5 * (seg[0][1] + seg[1][1])))
                    break
    if hits:
        nets = sorted({h[0] for h in hits})
        loc = ViolationLocation(layer=None, x_mm=hits[0][1], y_mm=hits[0][2],
                                notes=f"Trace over cutout on {hits[0][0]}.")
        msg = (f"{len(hits)} trace segment(s) routed over an internal board cutout on "
               f"{', '.join(nets[:6])} -> no support / no return path.")
        return advisory(ctx, True, count_metric(len(hits)), msg, location=loc)
    return advisory(ctx, False, count_metric(0), "No traces over board cutouts.")
