"""Design advisory: component pads too close to a board edge.

A part's body extends beyond its pads, so pads near an edge mean the component
overhangs or sits in the depaneling stress zone -- handling damage, connector
overhang, break-off cracks. A larger keep-back is expected for parts than for bare
copper (which copper_to_edge_distance already covers at the fab minimum). Advisory
only.

"Edge" means the real outline: the boundary contour *and* any internal cutout.
This measured to the board's bounding box until #107, which made it blind twice
over -- to cutouts, and to any concave boundary, so a part sitting in a notch or
outside an L-shaped edge read as comfortably interior.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry import queries
from ..results import CheckResult, MetricResult, Violation, ViolationLocation
from ._design_advisory import dist_to_edges, is_pad_like, na, outline_contours, point_inside


@register_check("component_edge_clearance")
def run_component_edge_clearance(ctx: CheckContext) -> CheckResult:
    limits = ctx.check_def.limits or {}
    recommended = float(limits.get("recommended_min", 0.5))
    absolute = float(limits.get("absolute_min", 0.3))

    boundary, cutouts = outline_contours(ctx)
    if boundary is None:
        # No assembled outline contour (unclosed or missing outline layer): fall
        # back to the bounding box rather than skipping the check entirely.
        board = queries.get_board_bounds(ctx.geometry)
        if board is None:
            return na(ctx, "No board outline to measure component-to-edge clearance.",
                      units="mm")
        boundary = [(board.min_x, board.min_y), (board.max_x, board.min_y),
                    (board.max_x, board.max_y), (board.min_x, board.max_y)]
        cutouts = []

    def clearance(b) -> float:
        """Pad-bounds to nearest real edge; negative when the pad crosses one.

        Measured from the bbox corners of the pad, so a pad that reaches an edge
        with any part of itself is caught, not just one whose centre is close.
        """
        corners: List[Tuple[float, float]] = [
            (b.min_x, b.min_y), (b.max_x, b.min_y),
            (b.max_x, b.max_y), (b.min_x, b.max_y),
        ]
        d = min(min(dist_to_edges(x, y, boundary) for x, y in corners),
                *( [min(dist_to_edges(x, y, cut) for x, y in corners)
                    for cut in cutouts] or [float("inf")] ))
        # Outside the boundary, or inside a cutout => the pad overhangs an edge.
        outside = any(not point_inside(boundary, x, y) for x, y in corners)
        in_void = any(point_inside(cut, x, y) for cut in cutouts for x, y in corners)
        return -d if (outside or in_void) else d

    min_clear: Optional[float] = None
    loc: Optional[ViolationLocation] = None
    for layer in queries.get_copper_layers(ctx.geometry):
        for poly in layer.polygons:
            if not is_pad_like(poly):
                continue
            b = poly.bounds()
            d = clearance(b)
            if min_clear is None or d < min_clear:
                min_clear = d
                loc = ViolationLocation(
                    layer=layer.logical_layer,
                    x_mm=0.5 * (b.min_x + b.max_x),
                    y_mm=0.5 * (b.min_y + b.max_y),
                    notes="Component pad closest to the board edge.",
                )

    if min_clear is None:
        return na(ctx, "No component pads found to measure edge clearance.", units="mm")

    flagged = min_clear < recommended
    metric = MetricResult.geometry_mm(
        measured_mm=float(min_clear), target_mm=recommended, limit_low_mm=absolute)
    status = "warning" if flagged else "pass"
    sev = "warning" if flagged else "info"
    msg = (f"Closest component pad is {min_clear:.3f} mm from the board edge, "
           f"below the recommended {recommended:.2f} mm keep-back; the part body "
           f"extends even closer. Pull edge components inward."
           if flagged else
           f"Component pads keep clear of the board edge (min {min_clear:.3f} mm).")
    return CheckResult(
        check_id=ctx.check_def.id, name=ctx.check_def.name,
        category_id=ctx.check_def.category_id, status=status, severity=sev,
        score=60.0 if flagged else 100.0, metric=metric,
        violations=[Violation(severity=sev, message=msg,
                              location=loc if flagged else None)],
    ).finalize()
