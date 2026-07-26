"""Design advisory: silkscreen clipped by the board edge.

Silk that straddles the outline gets trimmed at depaneling, leaving reference
designators or marks partly missing. We flag only silk whose centroid is ON the
board but whose geometry crosses the outline -- silk drawn entirely OFF the board
(title blocks, fab notes, rulers) is intentional documentation and ignored.
"""

from __future__ import annotations

from typing import List, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry import queries
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, board_contour_verts, count_metric, na, poly_area_mm2

_MIN_SILK_AREA_MM2 = 0.02   # skip specks / tessellation noise


@register_check("silkscreen_off_board")
def run_silkscreen_off_board(ctx: CheckContext) -> CheckResult:
    verts = board_contour_verts(ctx)
    if not verts or len(verts) < 3:
        return na(ctx, "No closed board outline available to test silk clipping.")

    from ._design_advisory import point_inside

    silk_layers = queries.get_silkscreen_layers(ctx.geometry)
    if not silk_layers:
        return na(ctx, "No silkscreen layer present.")

    clipped: List[Tuple[float, float]] = []
    for layer in silk_layers:
        for poly in getattr(layer, "polygons", []):
            if poly_area_mm2(poly) < _MIN_SILK_AREA_MM2:
                continue
            pv = poly.vertices
            if len(pv) < 2:
                continue
            b = poly.bounds()
            cx, cy = 0.5 * (b.min_x + b.max_x), 0.5 * (b.min_y + b.max_y)
            # Off-board documentation: centroid not on the board -> ignore.
            if not point_inside(verts, cx, cy):
                continue
            inside = outside = 0
            for v in pv:
                if point_inside(verts, v.x, v.y):
                    inside += 1
                else:
                    outside += 1
                if inside and outside:
                    break
            if inside and outside:
                clipped.append((cx, cy))

    count = len(clipped)
    if count == 0:
        return advisory(ctx, False, count_metric(0),
                        "No silkscreen features are clipped by the board edge.")
    x, y = clipped[0]
    return advisory(
        ctx, True, count_metric(count),
        f"{count} on-board silkscreen feature(s) cross the board outline and will "
        f"be trimmed at depaneling. Pull reference designators / marks inside the "
        f"edge keep-out.",
        ViolationLocation(layer="Silkscreen", x_mm=x, y_mm=y,
                          notes="Silkscreen clipped by board edge."),
    )
