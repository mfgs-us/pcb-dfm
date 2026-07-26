"""Design advisory: large isolated copper regions with no connection.

An orphaned copper blob -- not touching any other copper and carrying no drilled
hole -- is often an unintended routing leftover, and electrically it floats (an
antenna / EMI coupler). This is a screen, not a certainty: intentional isolated
copper exists (logos, shields, large thieving), so we stay conservative --
ignore small copper (thieving dots, fiducials), ignore anything with a drill in
it (mounting pads, via pads), and treat bbox-overlapping copper as connected so
a blob near other copper is never wrongly called isolated.
"""

from __future__ import annotations

from typing import List, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry import queries
from ..geometry.gerber_backend import GERBONARA_AVAILABLE, excellon_hits_mm
from ..geometry.polygon_index import PolygonIndex
from ..geometry.primitives import Bounds
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, bbox_center, count_metric, na, poly_area_mm2
from ._geometry_guard import implausible_extent_result

# Only mid-size isolated copper is a candidate: below this is thieving / a
# fiducial / a stray fragment; those are common and harmless.
_MIN_FLOATING_AREA_MM2 = 2.0


@register_check("floating_copper")
def run_floating_copper(ctx: CheckContext) -> CheckResult:
    guard = implausible_extent_result(ctx)
    if guard is not None:
        return guard

    copper_layers = queries.get_copper_layers(ctx.geometry)
    if not copper_layers:
        return na(ctx, "No copper layers to evaluate for floating copper.")

    # Drill hits (any hole -- plated vias, mounting holes) as bboxes.
    holes: List[Bounds] = []
    if GERBONARA_AVAILABLE:
        for f in getattr(ctx.ingest, "files", None) or []:
            if getattr(f, "layer_type", None) == "drill":
                for h in excellon_hits_mm(f.path):
                    r = 0.5 * h.diameter_mm
                    holes.append(Bounds(h.x_mm - r, h.y_mm - r, h.x_mm + r, h.y_mm + r))
    hole_index = PolygonIndex.from_bounds(list(enumerate(holes))) if holes else None

    flagged: List[Tuple[float, float, float]] = []  # (area, cx, cy)
    for layer in copper_layers:
        polys = list(layer.polygons)
        bounds = [p.bounds() for p in polys]
        # Index this layer's copper so "does anything else touch me?" is cheap.
        idx = PolygonIndex.from_bounds(list(enumerate(bounds)))
        for i, poly in enumerate(polys):
            b = bounds[i]
            area = poly_area_mm2(poly)
            if area < _MIN_FLOATING_AREA_MM2:
                continue
            # A drilled hole inside (via/mounting pad) means it is connected /
            # intentional -- not floating.
            if hole_index is not None and hole_index.query_bbox(b):
                continue
            # Connected if any OTHER copper on this layer overlaps its bbox.
            # (Over-counting connections only makes us more conservative.)
            neighbours = [j for j in idx.query_bbox(b) if j != i]
            if neighbours:
                continue
            cx, cy = bbox_center(b)
            flagged.append((area, cx, cy))

    count = len(flagged)
    if count == 0:
        return advisory(ctx, False, count_metric(0),
                        "No large isolated (floating) copper regions found.")
    flagged.sort(reverse=True)  # largest first
    area, cx, cy = flagged[0]
    return advisory(
        ctx, True, count_metric(count),
        f"{count} isolated copper region(s) with no drilled connection "
        f"(largest ~{area:.1f} mm^2). Verify each is intentional (logo/shield/"
        f"thieving); an unintended island floats and can couple noise.",
        ViolationLocation(layer="Copper", x_mm=cx, y_mm=cy,
                          notes="Isolated copper region."),
    )
