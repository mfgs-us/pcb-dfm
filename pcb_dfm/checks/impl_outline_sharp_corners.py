"""Design advisory: acute (spike) corners on the OUTER board boundary.

A spike on the outer edge is a handling / depaneling hazard (it snags, and a
sharp point is fragile). This is the exterior complement to
fillet_radius_milling, which covers INTERNAL milled corners. Ordinary 90 deg
rectangle corners are fine and are not flagged; only genuinely acute convex
corners are.
"""

from __future__ import annotations

import math

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, board_contour_verts, count_metric, na

_ACUTE_DEG = 60.0     # a convex corner sharper than this is a spike
_MIN_EDGE_MM = 0.3    # ignore arc-tessellation segments shorter than this


@register_check("outline_sharp_corners")
def run_outline_sharp_corners(ctx: CheckContext) -> CheckResult:
    verts = board_contour_verts(ctx)
    if not verts or len(verts) < 3:
        return na(ctx, "No closed board outline available to evaluate corners.")

    n = len(verts)
    signed = 0.0
    for i in range(n):
        x1, y1 = verts[i]
        x2, y2 = verts[(i + 1) % n]
        signed += x1 * y2 - x2 * y1
    ccw = signed > 0

    sharp = []  # (interior_angle, x, y)
    for i in range(n):
        px, py = verts[(i - 1) % n]
        cx, cy = verts[i]
        nx, ny = verts[(i + 1) % n]
        ax, ay = cx - px, cy - py
        bx, by = nx - cx, ny - cy
        la = math.hypot(ax, ay)
        lb = math.hypot(bx, by)
        if la < _MIN_EDGE_MM or lb < _MIN_EDGE_MM:
            continue
        # Convex vertices turn in the polygon's winding direction; concave ones
        # (internal notches) are fillet_radius_milling's concern, not this one.
        cross = ax * by - ay * bx
        if (cross > 0) != ccw:
            continue
        cosang = max(-1.0, min(1.0, (ax * bx + ay * by) / (la * lb)))
        interior = 180.0 - math.degrees(math.acos(cosang))
        if interior < _ACUTE_DEG:
            sharp.append((interior, cx, cy))

    count = len(sharp)
    if count == 0:
        return advisory(ctx, False, count_metric(0),
                        "No acute external outline corners.")
    sharp.sort()
    angle, x, y = sharp[0]
    return advisory(
        ctx, True, count_metric(count),
        f"{count} acute external board-outline corner(s); sharpest ~{angle:.0f} deg. "
        f"Chamfer or round outer spikes to ease handling and depaneling.",
        ViolationLocation(layer="Outline", x_mm=x, y_mm=y,
                          notes="Acute external outline corner."),
    )
