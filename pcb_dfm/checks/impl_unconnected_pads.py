"""Design advisory: a component pad on no net is almost always an error.

A pad that resolves to no net is an unrouted / disconnected pin -- the classic
"forgot to route it" or "ratsnest still showing" miss. Intentional no-net pads
(mounting holes, fiducials, test points, antenna elements) and do-not-populate
parts are excluded. Needs a netlist + component pad geometry; otherwise
not_applicable.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..ingest.design_intel import build_pad_net_index, classify_component
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, count_metric, na

# Classes whose pads are legitimately net-less (mechanical / intentional).
_NO_NET_OK = {"mounting", "fiducial", "testpoint", "antenna"}


@register_check("unconnected_pads")
def run_unconnected_pads(ctx: CheckContext) -> CheckResult:
    dd = getattr(ctx, "design_data", None)
    if dd is None or not dd.nets or not dd.components:
        return na(ctx, "Needs a netlist and component placement to resolve pads to nets.")
    if not any(c.pads for c in dd.components):
        return na(ctx, "Components carry no pad geometry; cannot resolve pads to nets.")

    idx = build_pad_net_index(dd)
    by_ref: Dict[str, object] = {c.ref: c for c in dd.components}

    flagged: List[Tuple[str, str, Optional[Tuple[float, float]]]] = []
    for (ref, pad_name) in idx.unmatched_pads:
        comp = by_ref.get(ref)
        if comp is None:
            continue
        if getattr(comp, "dnp", False):
            continue  # do-not-populate: an open pad is expected
        cls, _ = classify_component(comp)
        if cls in _NO_NET_OK:
            continue
        # A single-pad part with no class is almost always mechanical -- skip it
        # rather than risk a false positive on a mounting pad the BOM didn't tag.
        if cls is None and len(getattr(comp, "pads", [])) <= 1:
            continue
        loc = None
        for p in comp.pads:
            if p.name == pad_name:
                loc = (p.x_mm, p.y_mm)
                break
        flagged.append((ref, pad_name, loc))

    count = len(flagged)
    if count == 0:
        return advisory(ctx, False, count_metric(0),
                        "Every component pad resolves to a net.")
    ref, pad_name, loc = flagged[0]
    vloc = None
    if loc is not None:
        vloc = ViolationLocation(layer="Copper", x_mm=loc[0], y_mm=loc[1],
                                 notes=f"Pad {ref}.{pad_name} is on no net.")
    return advisory(
        ctx, True, count_metric(count),
        f"{count} component pad(s) resolve to no net (e.g. {ref}.{pad_name}) -- likely "
        f"unrouted or disconnected pins. Verify the ratsnest is fully routed "
        f"(mounting/fiducial/test-point/DNP pads are already excluded).",
        vloc,
    )
