"""Design advisory: breakout-risk via junctions that want a teardrop.

A teardrop (a fillet where a trace meets a via/pad) relieves drill breakout and
mechanical stress. Flagging the *absence* of teardrops in general would fire on
almost every board -- most don't use them -- which is noise, not signal. So this
flags only the junctions where a teardrop materially helps: small vias whose
annular ring is already thin (below the recommended ring), where drill wander
would break out without the extra copper a teardrop adds. Advisory only.
"""

from __future__ import annotations

import math
from typing import List, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry import queries
from ..geometry.gerber_backend import GERBONARA_AVAILABLE, excellon_hits_mm
from ..geometry.polygon_index import PolygonIndex
from ..geometry.primitives import Bounds
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, count_metric, na
from ._geometry_guard import implausible_extent_result
from .impl_min_annular_ring import _min_distance_to_polygon_edges, _point_in_polygon

_VIA_MAX_DRILL_MM = 0.5     # only small (via-sized) holes; big holes aren't teardropped
_THIN_RING_MM = 0.10       # only vias at/below the fab minimum ring (genuine breakout risk)


@register_check("teardrop_presence")
def run_teardrop_presence(ctx: CheckContext) -> CheckResult:
    guard = implausible_extent_result(ctx)
    if guard is not None:
        return guard

    if not GERBONARA_AVAILABLE:
        return na(ctx, "Gerber parser unavailable; cannot evaluate via junctions.")

    copper_layers = queries.get_copper_layers(ctx.geometry)
    drill_files = [f for f in (getattr(ctx.ingest, "files", None) or [])
                   if getattr(f, "layer_type", None) == "drill"
                   and getattr(f, "is_plated", None) is not False]
    if not copper_layers or not drill_files:
        return na(ctx, "No plated vias / copper to evaluate teardrop junctions.")

    vias: List[Tuple[float, float, float]] = []  # (x, y, radius)
    for f in drill_files:
        for h in excellon_hits_mm(f.path):
            if 0.0 < h.diameter_mm <= _VIA_MAX_DRILL_MM:
                vias.append((h.x_mm, h.y_mm, 0.5 * h.diameter_mm))
    if not vias:
        return na(ctx, "No via-sized plated holes to evaluate.")

    # Index copper so each via only checks the pads that could contain it.
    entries = []
    polys = []
    for layer in copper_layers:
        for poly in layer.polygons:
            polys.append(poly)
            b = poly.bounds()
            entries.append((len(polys) - 1, Bounds(b.min_x, b.min_y, b.max_x, b.max_y)))
    index = PolygonIndex.from_bounds(entries)

    at_risk: List[Tuple[float, float]] = []
    for (vx, vy, vr) in vias:
        best_ring = math.inf
        for pid in index.query_bbox(Bounds(vx, vy, vx, vy)):
            verts = polys[pid].vertices
            if not _point_in_polygon(vx, vy, verts):
                continue
            edge = _min_distance_to_polygon_edges(vx, vy, verts)
            if math.isfinite(edge):
                best_ring = min(best_ring, edge - vr)
        # A finite, thin ring on a via that sits inside a pad is the junction a
        # teardrop protects. (No containing pad -> not our concern here.)
        if math.isfinite(best_ring) and best_ring < _THIN_RING_MM:
            at_risk.append((vx, vy))

    count = len(at_risk)
    if count == 0:
        return advisory(ctx, False, count_metric(0),
                        "No thin-annular via junctions that would need a teardrop.")
    x, y = at_risk[0]
    return advisory(
        ctx, True, count_metric(count),
        f"{count} small via(s) have an annular ring below {_THIN_RING_MM * 1000:.0f} um (breakout risk); "
        f"add teardrops at these trace-to-via junctions to reduce drill-breakout risk.",
        ViolationLocation(layer="Copper", x_mm=x, y_mm=y,
                          notes="Thin-annular via junction (teardrop recommended)."),
    )
