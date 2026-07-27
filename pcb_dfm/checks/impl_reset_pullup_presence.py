"""Reset bias presence -- a reset net with no pull-up or RC can float."""

from __future__ import annotations

import re

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult
from ._design_advisory import advisory, count_metric, na
from ._design_review import resolve_design

_RST_RE = re.compile(r"(^|[_/-])(n?reset|n?rst|mclr|por|nrst_?out)([_/0-9-]|$)", re.I)


@register_check("reset_pullup_presence")
def run_reset_pullup_presence(ctx: CheckContext) -> CheckResult:
    r = resolve_design(ctx.design_data)
    if r is None:
        return na(ctx, "No netlist + BOM to resolve reset nets; not applicable.")
    nets = sorted(n for n in r.dd.nets if _RST_RE.search(n or ""))
    if not nets:
        return na(ctx, "No reset nets by name; not applicable.")
    if not r.power_nets():
        return na(ctx, "No power net identified; cannot review reset bias.")

    bad = []
    for n in nets:
        if r.resistor_to_power_on(n) or r.cap_to_ground_on(n):
            continue  # pull-up or RC bias present
        bad.append(n)
    flagged = bool(bad)
    msg = (f"Reset net(s) with no pull-up or RC bias: {', '.join(bad[:8])} "
           f"-> verify a defined idle level (or a supervisor drives it).") if flagged \
        else f"All {len(nets)} reset net(s) have a defined bias."
    return advisory(ctx, flagged, count_metric(len(bad)), msg)
