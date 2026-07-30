"""A component pad sitting over an internal board void.

``trace_over_cutout`` covers a trace routed across a cutout. A *pad* over one is
the same defect and was not covered: there is nothing to solder to, and the pad's
copper is unsupported right at the milled edge, where it is most likely to lift.

The discriminator this check lives or dies by: a component **body** over a cutout
is frequently correct -- that is exactly what "mid-mount" means, and a buzzer or a
recessed connector is *supposed* to sit in its opening. So the scope is pads only,
never courtyards or bodies, because a pad over a void is never intentional. And a
pad inside a cutout that its own footprint declared is intentional by
construction, so it is excluded outright.

Scoping it that way is not a simplification of the check -- it IS the check.
Without it, every correctly designed mid-mount connector on the board is a
finding.
"""

from __future__ import annotations

import math
from typing import List, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.gerber_backend import GERBONARA_AVAILABLE
from ..results import CheckResult, ViolationLocation
from ._design_advisory import (
    advisory,
    count_metric,
    in_declared_cutout,
    na,
    outline_contours,
    point_inside,
)
from ._trace_geom import segments_cross


def _pad_corners(pad) -> List[Tuple[float, float]]:
    """The pad's copper extent as a polygon, or a degenerate point when unsized.

    Rotation is applied about the pad centre, so a rotated connector finger is
    tested against the shape it actually occupies rather than an inflated box.
    """
    w, h = getattr(pad, "width_mm", None), getattr(pad, "height_mm", None)
    if not w or not h or w <= 0 or h <= 0:
        return [(pad.x_mm, pad.y_mm)]
    a = math.radians(getattr(pad, "rotation_deg", 0.0) or 0.0)
    ca, sa = math.cos(a), math.sin(a)
    hw, hh = w / 2.0, h / 2.0
    return [
        (pad.x_mm + lx * ca - ly * sa, pad.y_mm + lx * sa + ly * ca)
        for lx, ly in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))
    ]


def _overlaps(corners: List[Tuple[float, float]],
              cutout: List[Tuple[float, float]]) -> bool:
    """Does the pad's extent overlap the cutout polygon?

    Three ways, because any one alone misses a case: a pad corner inside the
    cutout (pad partly over the void), a cutout vertex inside the pad (a small
    slot fully under a large pad), or crossing edges (offset overlap with neither
    vertex set contained).
    """
    if any(point_inside(cutout, x, y) for x, y in corners):
        return True
    if len(corners) < 3:
        return False
    if any(point_inside(corners, x, y) for x, y in cutout):
        return True
    n, m = len(corners), len(cutout)
    for i in range(n):
        seg = (corners[i], corners[(i + 1) % n])
        for j in range(m):
            if segments_cross(seg, (cutout[j], cutout[(j + 1) % m])):
                return True
    return False


@register_check("pad_over_cutout")
def run_pad_over_cutout(ctx: CheckContext) -> CheckResult:
    dd = ctx.design_data
    if dd is None or not dd.components:
        return na(ctx, "No component pad geometry (needs design data); not applicable.")
    if not GERBONARA_AVAILABLE:
        return na(ctx, "Gerber parser unavailable; not applicable.")

    _boundary, cutouts = outline_contours(ctx)
    if not cutouts:
        return na(ctx, "Board outline has no internal cutouts; not applicable.")

    bad: List[Tuple[str, str, float, float]] = []
    for comp in dd.components:
        for pad in (comp.pads or []):
            corners = _pad_corners(pad)
            for cut in cutouts:
                if not _overlaps(corners, cut):
                    continue
                # A pad inside an opening this component's own footprint declared
                # is the mid-mount case working as designed.
                if in_declared_cutout(comp, pad.x_mm, pad.y_mm):
                    continue
                bad.append((comp.ref or "?", pad.name, pad.x_mm, pad.y_mm))
                break

    if not bad:
        return advisory(ctx, False, count_metric(0),
                        "No component pads sit over a board cutout.")

    bad.sort()
    refs = ", ".join(f"{ref}.{pin} at ({x:.1f}, {y:.1f})" for ref, pin, x, y in bad[:6])
    msg = (
        f"{len(bad)} component pad(s) overlap an internal board cutout: {refs}. "
        f"There is no laminate under the pad to solder to, and its copper is "
        f"unsupported at the milled edge."
    )
    loc = ViolationLocation(layer=None, x_mm=bad[0][2], y_mm=bad[0][3],
                            notes=f"Pad {bad[0][0]}.{bad[0][1]} over a cutout.")
    return advisory(ctx, True, count_metric(len(bad)), msg, location=loc)
