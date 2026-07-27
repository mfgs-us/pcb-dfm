"""I2C pull-up presence -- open-drain SDA/SCL need a pull-up to a rail."""

from __future__ import annotations

import re

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult
from ._design_advisory import advisory, count_metric, na
from ._design_review import resolve_design

_I2C_RE = re.compile(r"(^|[_/-])(sda|scl|i2c|smb(dat|clk))([_/0-9-]|$)", re.I)


@register_check("i2c_pullup_presence")
def run_i2c_pullup_presence(ctx: CheckContext) -> CheckResult:
    r = resolve_design(ctx.design_data)
    if r is None:
        return na(ctx, "No netlist + BOM to resolve I2C nets; not applicable.")
    nets = sorted(n for n in r.dd.nets if _I2C_RE.search(n or ""))
    if not nets:
        return na(ctx, "No I2C (SDA/SCL) nets by name; not applicable.")
    if not r.power_nets():
        return na(ctx, "No power net identified; cannot review pull-ups.")

    bad = [n for n in nets if not r.resistor_to_power_on(n)]
    flagged = bool(bad)
    msg = (f"I2C net(s) with no pull-up resistor to a rail: {', '.join(bad[:8])} "
           f"-> open-drain bus won't idle high.") if flagged \
        else f"All {len(nets)} I2C net(s) have a pull-up."
    return advisory(ctx, flagged, count_metric(len(bad)), msg)
