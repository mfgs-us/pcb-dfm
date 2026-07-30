"""Tall part edge clearance -- a tall body crowding a board edge.

"Board edge" includes internal cutouts and slots, not just the outer boundary. A
tall part standing at the lip of a cutout has exactly the handling and depanel
exposure the outer edge does, and measuring only to the boundary missed it
entirely -- the same blind spot `copper_to_edge_distance` and `trace_over_cutout`
already avoid on the copper side.
"""

from __future__ import annotations

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, ViolationLocation
from ._design_advisory import (
    advisory,
    count_metric,
    dist_to_edges,
    in_declared_cutout,
    na,
    outline_contours,
    point_inside,
)


@register_check("tall_part_edge_clearance")
def run_tall_part_edge_clearance(ctx: CheckContext) -> CheckResult:
    dd = ctx.design_data
    if dd is None or not dd.components:
        return na(ctx, "No components; not applicable.")
    tall = float((ctx.check_def.raw.get("params", {}) or {}).get("tall_mm", 5.0))
    margin = float((ctx.check_def.raw.get("params", {}) or {}).get("edge_margin_mm", 2.0))

    withh = [c for c in dd.components if c.height_mm and c.x_mm is not None]
    if not withh:
        return na(ctx, "No component heights in the BOM; tall-part clearance not applicable.")
    verts, cutouts = outline_contours(ctx)
    if not verts:
        bb = ctx.geometry.board_bounds() if ctx.geometry is not None else None
        if bb is None:
            return na(ctx, "No board outline; not applicable.")
        verts = [(bb.min_x, bb.min_y), (bb.max_x, bb.min_y),
                 (bb.max_x, bb.max_y), (bb.min_x, bb.max_y)]
        cutouts = []

    bad = []
    for c in withh:
        if c.height_mm < tall:
            continue
        if not point_inside(verts, c.x_mm, c.y_mm):
            continue
        # A part seated in an opening its own footprint declared (a mid-mount
        # connector) is not crowding that edge -- it is meant to be there.
        if in_declared_cutout(c, c.x_mm, c.y_mm):
            continue
        d = dist_to_edges(c.x_mm, c.y_mm, verts)
        for cut in cutouts:
            d = min(d, dist_to_edges(c.x_mm, c.y_mm, cut))
        if d < margin:
            bad.append((c.ref, c.height_mm, d, c.x_mm, c.y_mm))
    bad.sort()
    flagged = bool(bad)
    if flagged:
        loc = ViolationLocation(layer=None, x_mm=bad[0][3], y_mm=bad[0][4],
                                notes=f"Tall part {bad[0][0]} near the edge.")
        msg = ("Tall component(s) within " + f"{margin:.1f} mm of the edge: "
               + ", ".join(f"{ref} ({h:.1f} mm tall, {d:.1f} mm to edge)"
                           for ref, h, d, _x, _y in bad[:6]) + ".")
        return advisory(ctx, True, count_metric(len(bad)), msg, location=loc)
    return advisory(ctx, False, count_metric(0), "No tall parts crowding the board edge.")
