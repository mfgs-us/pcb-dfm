"""Design advisory: a power/ground net crossing layers on a single via is a SPOF.

A power or ground rail that transitions layers through exactly one via funnels
all of its current -- and its reliability -- through that one barrel. A cracked
via or a marginal drill then kills the rail. Fabs and reviewers want redundant
(stitched) vias on power/ground layer transitions.

Needs a design source that carries via topology per net (e.g. a KiCad board).
IPC-D-356 netlists carry access points but no vias, so this is not_applicable
there.
"""

from __future__ import annotations

from typing import List, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..ingest.design_intel import is_power_or_ground
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, count_metric, na


@register_check("power_feed_robustness")
def run_power_feed_robustness(ctx: CheckContext) -> CheckResult:
    dd = getattr(ctx, "design_data", None)
    if dd is None or not dd.nets:
        return na(ctx, "Needs a netlist to identify power/ground nets.")
    if not any(n.vias for n in dd.nets.values()):
        return na(ctx, "No via topology in the design data (IPC-D-356 carries no "
                       "vias); supply a source with vias to evaluate.")

    flagged: List[Tuple[str, float, float]] = []
    for net in dd.nets.values():
        if not is_power_or_ground(net.name, net.net_class):
            continue
        if len(net.vias) != 1:
            continue  # 0 vias = single layer; >=2 = already redundant
        v = net.vias[0]
        flagged.append((net.name, v.x_mm, v.y_mm))

    count = len(flagged)
    if count == 0:
        return advisory(ctx, False, count_metric(0),
                        "No power/ground net relies on a single layer-transition via.")
    name, x, y = flagged[0]
    return advisory(
        ctx, True, count_metric(count),
        f"{count} power/ground net(s) cross layers through a single via "
        f"(e.g. '{name}') -- a current + reliability single-point-of-failure. "
        f"Stitch redundant vias at power/ground layer transitions.",
        ViolationLocation(layer="Copper", x_mm=x, y_mm=y,
                          notes=f"Net '{name}' has one layer-transition via."),
    )
