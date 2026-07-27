"""Debug port test access -- SWD/JTAG nets should have a TP or header."""

from __future__ import annotations

import re

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult
from ._design_advisory import advisory, count_metric, na
from ._design_review import resolve_design

# Strong debug/programming signal names. Generic tx/rx are excluded on purpose --
# too many ordinary UARTs don't need bring-up access, which would be noisy.
_DBG_RE = re.compile(
    r"(^|[_/-])(swdio|swclk|swo|swdck|tck|tms|tdi|tdo|trst|jtag|swd)([_/0-9-]|$)", re.I)


@register_check("debug_port_test_access")
def run_debug_port_test_access(ctx: CheckContext) -> CheckResult:
    r = resolve_design(ctx.design_data)
    if r is None:
        return na(ctx, "No netlist + BOM to resolve debug nets; not applicable.")
    nets = sorted(n for n in r.dd.nets if _DBG_RE.search(n or ""))
    if not nets:
        return na(ctx, "No SWD/JTAG debug nets by name; not applicable.")

    bad = [n for n in nets
           if not (r.has_class_on(n, "testpoint") or r.has_class_on(n, "connector"))]
    flagged = bool(bad)
    msg = (f"Debug net(s) with no test point or header for bring-up: "
           f"{', '.join(bad[:8])}.") if flagged \
        else f"All {len(nets)} debug net(s) have test/header access."
    return advisory(ctx, flagged, count_metric(len(bad)), msg)
