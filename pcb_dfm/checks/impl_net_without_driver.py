"""Net with no driver -- a signal net of only input pins is never driven.

Needs schematic pin electrical types; not_applicable without them (a bare-Gerber
board can't tell an input pin from an output). Conservative: a net with any
driver pin OR any biasing passive (a pull resistor gives a defined level) is
fine, so only a net of pure inputs is a floating-input error.
"""

from __future__ import annotations

from typing import List

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult
from ._design_advisory import advisory, count_metric, na
from ._design_review import resolve_design

_DRIVERS = {"output", "bidirectional", "tri_state", "open_collector",
            "open_emitter", "power_out", "power_in"}
# A "receiver-only" set: if the net has nothing but these, nothing drives it.
_RECEIVER_ONLY = {"input"}


@register_check("net_without_driver")
def run_net_without_driver(ctx: CheckContext) -> CheckResult:
    rd = resolve_design(ctx.design_data)
    if rd is None:
        return na(ctx, "No netlist + BOM to resolve nets; not applicable.")
    if not rd.has_pin_types():
        return na(ctx, "No schematic pin types; no-driver review not applicable.")

    bad: List[str] = []
    for net, types in rd.net_pin_types.items():
        if rd.net_func.get(net) != "signal":
            continue
        if not (types & _RECEIVER_ONLY):
            continue                    # no input pin -> not our concern
        if types & _DRIVERS:
            continue                    # a driver is present
        if "passive" in types:
            continue                    # a pull/series passive can bias it
        # Only inputs (+ maybe unspecified/free/no_connect) -> nothing drives it.
        if rd.pins_on_net.get(net, 0) >= 2:
            bad.append(net)
    bad.sort()
    flagged = bool(bad)
    msg = (f"{len(bad)} signal net(s) reach only input pins with no driver: "
           f"{', '.join(bad[:8])} -> floating input(s).") if flagged \
        else "Every signal net has a driver or bias."
    return advisory(ctx, flagged, count_metric(len(bad)), msg)
