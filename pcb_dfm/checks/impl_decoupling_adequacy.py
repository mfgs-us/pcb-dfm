"""Decoupling adequacy per rail -- the first electrical-correctness review check.

A power rail that reaches an IC supply pin but has no bypass capacitor to ground
is a classic peer-review catch: supply noise, brown-out, marginal behaviour. It is
also a strong data-completeness signal.

This is a *design review* finding, so it follows the stricter review contract
(see docs/design_reviewer.md): advisory only, grounded in resolved facts (the
netlist + BOM via ``PadNetIndex`` and the ``design_intel`` classifiers), and
``not_applicable`` -- never a guess -- when the inputs to resolve rails and parts
are missing.

Conservative by design: it flags only the high-confidence case -- a rail with IC
supply pins and **zero** capacitors to ground (any value, so a bulk-only input
rail is not a false positive). Rails that reach no IC (connector/passive rails)
are not a decoupling concern and are skipped. The bypass-cap-to-pin ratio is
reported for context but does not, on its own, raise a flag in this version.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..ingest.design_intel import (
    build_pad_net_index,
    classify_component,
    classify_net,
)
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, count_metric, na


@register_check("decoupling_adequacy")
def run_decoupling_adequacy(ctx: CheckContext) -> CheckResult:
    dd = ctx.design_data
    if dd is None or not dd.nets or not dd.components:
        return na(ctx, "No netlist + BOM to resolve rails and parts; decoupling adequacy not applicable.")

    idx = build_pad_net_index(dd)
    if not idx.pad_net:
        return na(ctx, "Component pads could not be matched to nets (no netlist access points); not applicable.")

    net_func: Dict[str, str] = {
        name: classify_net(name, net.net_class) for name, net in dd.nets.items()}
    if not any(f == "ground" for f in net_func.values()):
        return na(ctx, "No ground net identified; cannot assess decoupling to ground.")

    comp_by_ref = {c.ref: c for c in dd.components}
    part_class: Dict[str, Optional[str]] = {
        ref: classify_component(c)[0] for ref, c in comp_by_ref.items()}

    # IC supply pins per power net.
    ic_pins: Dict[str, int] = defaultdict(int)
    for (ref, _pad), net in idx.pad_net.items():
        if part_class.get(ref) == "ic" and net_func.get(net) == "power":
            ic_pins[net] += 1

    if not ic_pins:
        return na(ctx, "No IC supply rails resolved from the netlist; not applicable.")

    # Capacitors that bridge a power net to a ground net = bypass caps for that rail.
    bypass: Dict[str, Set[str]] = defaultdict(set)  # power net -> {cap refs}
    for ref, cls in part_class.items():
        if cls != "capacitor":
            continue
        nets = idx.nets_of(ref)
        powers = [n for n in nets if net_func.get(n) == "power"]
        grounds = [n for n in nets if net_func.get(n) == "ground"]
        if not grounds:
            continue
        for pn in powers:
            bypass[pn].add(ref)

    rails = sorted(ic_pins)
    undecoupled: List[Tuple[str, int]] = [
        (net, ic_pins[net]) for net in rails if not bypass.get(net)]

    # Worst bypass-cap-to-supply-pin ratio, for context in the report.
    worst_ratio = min(
        (len(bypass.get(net, ())) / ic_pins[net] for net in rails), default=None)

    flagged = bool(undecoupled)
    metric = count_metric(len(undecoupled))

    if flagged:
        listed = ", ".join(f"{net} ({pins} pin(s))" for net, pins in undecoupled[:6])
        more = "" if len(undecoupled) <= 6 else f" +{len(undecoupled) - 6} more"
        message = (
            f"{len(undecoupled)} of {len(rails)} IC supply rail(s) have no bypass "
            f"capacitor to ground: {listed}{more}. Add local decoupling (typ. "
            f"0.1 µF per supply pin)."
        )
        # Anchor at an access point on the worst rail, when the netlist carries one.
        loc = None
        worst_net = undecoupled[0][0]
        pts = dd.nets[worst_net].points if worst_net in dd.nets else []
        if pts:
            loc = ViolationLocation(layer=None, x_mm=pts[0].x_mm, y_mm=pts[0].y_mm,
                                    notes=f"Undecoupled supply rail {worst_net}.")
        return advisory(ctx, True, metric, message, location=loc)

    ratio_txt = "" if worst_ratio is None else f" (worst {worst_ratio:.2f} caps/pin)"
    return advisory(
        ctx, False, metric,
        f"All {len(rails)} IC supply rail(s) have bypass capacitors to ground{ratio_txt}.")
