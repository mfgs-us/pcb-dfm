"""Design advisory: keep copper out of a designated antenna / RF region.

Copper (a ground pour, a stray trace) inside an antenna's keep-out detunes it and
kills range -- an expensive, silent mistake on a wireless board. Needs the
antenna region to be designated as a design-data keep-out (kind 'antenna'/'rf',
via a sidecar `keepouts` entry). Not_applicable when none is supplied.
"""

from __future__ import annotations

from typing import List, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, count_metric, na
from ._design_data_geo import copper_in_polygon


@register_check("antenna_keepout")
def run_antenna_keepout(ctx: CheckContext) -> CheckResult:
    dd = getattr(ctx, "design_data", None)
    regions = [
        k for k in (getattr(dd, "keepouts", None) or [])
        if str(k.kind).lower() in ("antenna", "rf") and len(k.polygon) >= 3
    ]
    if not regions:
        return na(ctx, "No antenna/RF keep-out region designated; supply one via a "
                       "design-data 'keepouts' entry to evaluate.")

    hits: List[Tuple[str, str, float, float]] = []
    for r in regions:
        for (lname, cx, cy) in copper_in_polygon(ctx.geometry, r.polygon, r.layers):
            hits.append((r.name or r.kind, lname, cx, cy))

    count = len(hits)
    if count == 0:
        n = len(regions)
        return advisory(ctx, False, count_metric(0),
                        f"{n} antenna/RF keep-out region(s) are clear of copper.")
    region_name, lname, x, y = hits[0]
    return advisory(
        ctx, True, count_metric(count),
        f"{count} copper feature(s) inside the antenna/RF keep-out "
        f"('{region_name}'); copper here detunes the antenna and cuts range. "
        f"Clear the region (or exclude the antenna feed line).",
        ViolationLocation(layer=lname, x_mm=x, y_mm=y,
                          notes="Copper inside antenna keep-out region."),
    )
