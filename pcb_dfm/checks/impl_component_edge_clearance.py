"""Design advisory: component pads too close to the board edge.

A part's body extends beyond its pads, so pads near the outline mean the
component overhangs or sits in the depaneling stress zone -- handling damage,
connector overhang, break-off cracks. A larger keep-back is expected for parts
than for bare copper (which copper_to_edge_distance already covers at the fab
minimum). Advisory only.
"""

from __future__ import annotations

from typing import Optional

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry import queries
from ..results import CheckResult, MetricResult, Violation, ViolationLocation
from ._design_advisory import is_pad_like, na


@register_check("component_edge_clearance")
def run_component_edge_clearance(ctx: CheckContext) -> CheckResult:
    limits = ctx.check_def.limits or {}
    recommended = float(limits.get("recommended_min", 0.5))
    absolute = float(limits.get("absolute_min", 0.3))

    board = queries.get_board_bounds(ctx.geometry)
    if board is None:
        return na(ctx, "No board outline to measure component-to-edge clearance.", units="mm")

    min_clear: Optional[float] = None
    loc: Optional[ViolationLocation] = None
    for layer in queries.get_copper_layers(ctx.geometry):
        for poly in layer.polygons:
            if not is_pad_like(poly):
                continue
            b = poly.bounds()
            # Distance from this pad to each board edge; negative => overhang.
            d = min(b.min_x - board.min_x, board.max_x - b.max_x,
                    b.min_y - board.min_y, board.max_y - b.max_y)
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
