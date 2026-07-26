"""Design advisory: a decoupling cap should sit close to the IC pin it serves.

A bypass/decoupling capacitor placed far from its IC's power pin loses its job --
the loop inductance to the pin swamps the cap. "Put the 100 nF right at the pin"
is the oldest rule in power integrity, and the easiest to miss in a dense layout.

Heuristic and conservative: caps are identified by value/refdes, an IC
association is only claimed when the cap and an IC share a *power* net (ground is
everywhere and would over-match). Needs a netlist + component pad placement;
otherwise, or when no cap shares a power net with an IC, not_applicable.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..ingest.design_intel import (
    build_pad_net_index,
    classify_component,
    classify_net,
    is_decoupling_candidate,
)
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, dist_metric, na

_TARGET_MM = 3.0   # good practice: cap within ~3 mm of the pin
_LIMIT_MM = 5.0    # beyond this the bypass is likely ineffective -> flag


def _dist(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


@register_check("decoupling_proximity")
def run_decoupling_proximity(ctx: CheckContext) -> CheckResult:
    dd = getattr(ctx, "design_data", None)
    if dd is None or not dd.nets or not dd.components:
        return na(ctx, "Needs a netlist and component placement to evaluate.")
    if not any(c.pads for c in dd.components):
        return na(ctx, "Components carry no pad geometry; cannot measure distances.")

    idx = build_pad_net_index(dd)
    pad_xy: Dict[Tuple[str, str], Tuple[float, float]] = {
        (c.ref, p.name): (p.x_mm, p.y_mm) for c in dd.components for p in c.pads
    }
    net_pads: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for (ref, pad), net in idx.pad_net.items():
        net_pads[net].append((ref, pad))

    ic_refs = {c.ref for c in dd.components if classify_component(c)[0] == "ic"}
    decaps = [c for c in dd.components if is_decoupling_candidate(c)]
    if not ic_refs or not decaps:
        return na(ctx, "No IC / decoupling-cap pair to evaluate.")

    # For each decap, the nearest served IC power pin across the power nets it shares.
    evaluated: List[Tuple[str, float, Tuple[float, float]]] = []
    for cap in decaps:
        power_nets = [n for n in idx.nets_of(cap.ref) if classify_net(n) == "power"]
        best: Optional[float] = None
        best_at: Optional[Tuple[float, float]] = None
        for pnet in power_nets:
            cap_pads = [pad_xy[(cap.ref, pad)] for (r, pad) in net_pads[pnet]
                        if r == cap.ref and (cap.ref, pad) in pad_xy]
            ic_pads = [pad_xy[(r, pad)] for (r, pad) in net_pads[pnet]
                       if r in ic_refs and r != cap.ref and (r, pad) in pad_xy]
            if not cap_pads or not ic_pads:
                continue
            for cp in cap_pads:
                d = min(_dist(cp, ip) for ip in ic_pads)
                if best is None or d < best:
                    best, best_at = d, cp
        if best is not None and best_at is not None:
            evaluated.append((cap.ref, best, best_at))

    if not evaluated:
        return na(ctx, "No decoupling cap shares a power net with an IC; nothing to measure.")

    # The worst-served decap drives the result.
    ref, worst, at = max(evaluated, key=lambda e: e[1])
    beyond = sum(1 for _, d, _ in evaluated if d > _TARGET_MM)
    flagged = worst > _LIMIT_MM
    metric = dist_metric(worst, _TARGET_MM)
    if not flagged:
        return advisory(ctx, False, metric,
                        f"All {len(evaluated)} decoupling cap(s) sit within "
                        f"{_LIMIT_MM:.0f} mm of a served IC power pin "
                        f"(worst {worst:.2f} mm).")
    return advisory(
        ctx, True, metric,
        f"Decoupling cap {ref} sits {worst:.2f} mm from its nearest served IC power "
        f"pin ({beyond} cap(s) beyond {_TARGET_MM:.0f} mm). Move bypass caps right up "
        f"to the pin to cut loop inductance.",
        ViolationLocation(layer="Copper", x_mm=at[0], y_mm=at[1],
                          notes=f"Decoupling cap {ref} far from its IC power pin."),
    )
