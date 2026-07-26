"""Design advisory: every net should have a probe-accessible test point.

ICT / flying-probe testing can only verify a net it can physically touch. A net
whose access points are all covered by solder mask (tented vias, buried
connections) is untestable. Needs a netlist (net access points) and a mask layer;
otherwise not_applicable.
"""

from __future__ import annotations

from typing import List, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, count_metric, na
from ._design_data_geo import has_mask_layer, mask_opening_lookup


@register_check("test_point_coverage")
def run_test_point_coverage(ctx: CheckContext) -> CheckResult:
    dd = getattr(ctx, "design_data", None)
    if dd is None or not dd.nets:
        return na(ctx, "No netlist supplied; test-point coverage needs net access points.")
    if not has_mask_layer(ctx.geometry):
        return na(ctx, "No solder-mask layer; cannot determine probe accessibility.")

    exposed = mask_opening_lookup(ctx.geometry)
    untestable: List[Tuple[str, float, float]] = []
    evaluated = 0
    for net in dd.nets.values():
        pts = net.points or []
        if not pts:
            continue
        evaluated += 1
        if not any(exposed(p.x_mm, p.y_mm) for p in pts):
            untestable.append((net.name, pts[0].x_mm, pts[0].y_mm))

    if evaluated == 0:
        return na(ctx, "Netlist carries no access points to evaluate.")

    count = len(untestable)
    if count == 0:
        return advisory(ctx, False, count_metric(0),
                        f"All {evaluated} evaluated nets have a probe-accessible test point.")
    name, x, y = untestable[0]
    return advisory(
        ctx, True, count_metric(count),
        f"{count} of {evaluated} net(s) have no solder-mask-exposed access point "
        f"(e.g. '{name}') -- untestable by flying probe / ICT. Add a test point or "
        f"expose a via.",
        ViolationLocation(layer="Copper", x_mm=x, y_mm=y,
                          notes=f"Net '{name}' has no accessible test point."),
    )
