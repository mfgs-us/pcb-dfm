"""Open-drain pull-up presence -- from schematic pin types.

An open-collector / open-emitter net can only pull one way; it needs a pull-up
(or pull-down) resistor to a rail to define the other level. This uses the
schematic pin types to find *every* open-drain net -- far more accurate than
matching names -- and defers to ``i2c_pullup_presence`` (name-based) as the
no-schematic fallback for SDA/SCL, so those aren't double-reported here.
"""

from __future__ import annotations

import re
from typing import List

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult
from ._design_advisory import advisory, count_metric, na
from ._design_review import resolve_design

_OPEN_DRAIN = {"open_collector", "open_emitter"}
_I2C_RE = re.compile(r"(^|[_/-])(sda|scl|i2c|smb)", re.I)


@register_check("open_drain_pullup")
def run_open_drain_pullup(ctx: CheckContext) -> CheckResult:
    rd = resolve_design(ctx.design_data)
    if rd is None:
        return na(ctx, "No netlist + BOM to resolve nets; not applicable.")
    if not rd.has_pin_types():
        return na(ctx, "No schematic pin types; open-drain review not applicable "
                       "(I2C is covered by i2c_pullup_presence).")
    if not rd.power_nets():
        return na(ctx, "No power net identified; cannot review pull-ups.")

    bad: List[str] = []
    for net, types in rd.net_pin_types.items():
        if not (types & _OPEN_DRAIN):
            continue
        if _I2C_RE.search(net or ""):
            continue  # SDA/SCL -> i2c_pullup_presence owns these
        if not rd.resistor_to_power_on(net):
            bad.append(net)
    bad.sort()
    flagged = bool(bad)
    msg = (f"{len(bad)} open-drain net(s) with no pull-up to a rail: "
           f"{', '.join(bad[:8])} -> line can't idle high.") if flagged \
        else "All open-drain nets have a pull-up."
    return advisory(ctx, flagged, count_metric(len(bad)), msg)
