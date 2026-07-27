"""Floating / single-pin net -- a net that reaches only one pin is a stub."""

from __future__ import annotations

import re

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult
from ._design_advisory import advisory, count_metric, na
from ._design_review import resolve_design

# Nets deliberately left with one terminal: no-connects, test/mounting stubs.
_NC_RE = re.compile(r"(^|[_/-])(nc|dnc|no_?connect|unconnected|open|nu)([_/0-9-]|$)", re.I)
_SKIP_ONLY = {"fiducial", "mounting", "testpoint"}


@register_check("floating_or_single_pin_net")
def run_floating_or_single_pin_net(ctx: CheckContext) -> CheckResult:
    r = resolve_design(ctx.design_data)
    if r is None:
        return na(ctx, "No netlist + BOM to resolve nets; single-pin-net review not applicable.")
    single = []
    for net, pins in r.pins_on_net.items():
        if pins != 1 or _NC_RE.search(net or ""):
            continue
        # Only signal nets: a single-pin power/ground net is usually an
        # intentional shield/mounting/unused-rail tie, not a forgotten connection.
        if r.net_func.get(net) != "signal":
            continue
        classes = {c for c in r.classes_on(net) if c is not None}
        if classes and classes <= _SKIP_ONLY:
            continue  # a lone test point / mounting hole net is not a stub
        single.append(net)
    single.sort()
    flagged = bool(single)
    if flagged:
        shown = ", ".join(single[:8]) + ("" if len(single) <= 8 else f" +{len(single) - 8} more")
        msg = f"{len(single)} net(s) reach only one pin (stub / unfinished connection): {shown}."
    else:
        msg = "No single-pin nets."
    return advisory(ctx, flagged, count_metric(len(single)), msg)
