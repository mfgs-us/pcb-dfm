"""Critical (power) pin connectivity.

Combines the schematic pin types (which pins are power) with the netlist
membership to flag power connectivity failures -- the NC-independent ones, so it
stays trustworthy without parsing no-connect markers:

  * a ``power_in`` pin on a *dead-end* net (a net with nothing else on it): it
    was wired to a net that supplies nothing; and
  * a component whose ``power_in`` pins reach *no* rail at all -> a wholly
    unpowered part (never intentional).

A power pin simply left with no net is deliberately NOT flagged here -- that is
the no-connect-ambiguous case (an intentionally-unused module power pin looks the
same as a forgotten one) and needs the schematic's no-connect markers.
Needs schematic pin types; not applicable without them.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..ingest.design_intel import classify_net
from ..results import CheckResult
from ._design_advisory import advisory, count_metric, na
from ._design_review import resolve_design


@register_check("critical_pin_connectivity")
def run_critical_pin_connectivity(ctx: CheckContext) -> CheckResult:
    dd = ctx.design_data
    if dd is None or not dd.pin_types:
        return na(ctx, "No schematic pin types; critical-pin connectivity not applicable.")
    rd = resolve_design(dd)
    if rd is None:
        return na(ctx, "No netlist + BOM to resolve pins; not applicable.")

    # A net is a real rail if its NAME says so (not the pin-enriched net_func --
    # that would be circular here, since a net with any power pin is "power"), or
    # it carries a power source (a power_out pin).
    power_out_nets = {rd.idx.pad_net.get((r, p))
                      for (r, p), t in dd.pin_types.items() if t == "power_out"}
    power_out_nets.discard(None)

    def _is_rail(net: str) -> bool:
        return (classify_net(net, rd.dd.nets[net].net_class if net in rd.dd.nets else None)
                in ("power", "ground")) or net in power_out_nets

    reaches_rail: Dict[str, bool] = defaultdict(bool)
    has_power_pin: Dict[str, bool] = defaultdict(bool)
    dead_end: List[str] = []
    forgotten: List[str] = []   # unconnected power pin that isn't marked no-connect
    reviewed = 0
    for (ref, pin), et in dd.pin_types.items():
        if et != "power_in":
            continue
        comp = rd.comp_by_ref.get(ref)
        if comp is None or not any(p.name == pin for p in comp.pads):
            continue  # not placed
        has_power_pin[ref] = True
        reviewed += 1
        net = rd.idx.pad_net.get((ref, pin))
        if net is None:
            # Unconnected. With no-connect markers we can tell a forgotten power
            # pin from an intentionally-unused one; without them, stay silent.
            if not rd.is_nc(ref, pin):
                forgotten.append(f"{ref}.{pin}")
            continue
        if rd.pins_on_net.get(net, 0) < 2:
            dead_end.append(f"{ref}.{pin} ({net})")
        if _is_rail(net):
            reaches_rail[ref] = True

    if reviewed == 0:
        return na(ctx, "No placed power pins to review; not applicable.")

    unpowered = sorted(ref for ref in has_power_pin
                       if not reaches_rail[ref]
                       # exclude a part whose only power pins are unconnected
                       # (all no-net) -- that's the NC-ambiguous case
                       and any(rd.idx.pad_net.get((ref, p.name)) is not None
                               and dd.pin_types.get((ref, p.name)) == "power_in"
                               for p in rd.comp_by_ref[ref].pads))
    dead_end.sort()
    forgotten.sort()
    total = len(unpowered) + len(dead_end) + len(forgotten)
    if flagged := (total > 0):
        bits = []
        if unpowered:
            bits.append(f"{len(unpowered)} part(s) with no powered rail: {', '.join(unpowered[:6])}")
        if forgotten:
            bits.append(f"{len(forgotten)} unconnected power pin(s) (not no-connect): {', '.join(forgotten[:6])}")
        if dead_end:
            bits.append(f"{len(dead_end)} power pin(s) on a dead-end net: {', '.join(dead_end[:6])}")
        msg = "Power connectivity issue -- " + "; ".join(bits) + "."
    else:
        msg = "All parts' power pins reach a rail."
    return advisory(ctx, flagged, count_metric(total), msg)
