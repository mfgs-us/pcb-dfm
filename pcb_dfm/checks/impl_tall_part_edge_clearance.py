"""Tall part edge clearance -- a tall body crowding the board edge."""

from __future__ import annotations

from math import hypot, inf

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, board_contour_verts, count_metric, na, point_inside


def _dist_to_edges(x: float, y: float, verts) -> float:
    best = inf
    n = len(verts)
    for i in range(n):
        x1, y1 = verts[i]
        x2, y2 = verts[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        if dx == 0 and dy == 0:
            d = hypot(x - x1, y - y1)
        else:
            t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
            d = hypot(x - (x1 + t * dx), y - (y1 + t * dy))
        best = min(best, d)
    return best


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
    verts = board_contour_verts(ctx)
    if not verts:
        bb = ctx.geometry.board_bounds() if ctx.geometry is not None else None
        if bb is None:
            return na(ctx, "No board outline; not applicable.")
        verts = [(bb.min_x, bb.min_y), (bb.max_x, bb.min_y),
                 (bb.max_x, bb.max_y), (bb.min_x, bb.max_y)]

    bad = []
    for c in withh:
        if c.height_mm < tall:
            continue
        if not point_inside(verts, c.x_mm, c.y_mm):
            continue
        d = _dist_to_edges(c.x_mm, c.y_mm, verts)
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
