"""Power-rail name aliasing -- two power nets that are probably one rail."""

from __future__ import annotations

import re
from collections import defaultdict

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..ingest.design_intel import classify_net
from ..results import CheckResult
from ._design_advisory import advisory, count_metric, na


def _key(name: str) -> str:
    # Strip a leading polarity sign and all separators/punctuation, lower-case.
    # "+3V3" and "3V3" collapse to "3v3"; "VCC" and "VCC_IO" stay distinct.
    return re.sub(r"[^a-z0-9]", "", name.lower().lstrip("+-"))


@register_check("rail_name_aliasing")
def run_rail_name_aliasing(ctx: CheckContext) -> CheckResult:
    dd = ctx.design_data
    if dd is None or not dd.nets:
        return na(ctx, "No nets; not applicable.")

    groups = defaultdict(set)
    for name, net in dd.nets.items():
        if classify_net(name, net.net_class) != "power":
            continue
        k = _key(name)
        if k:
            groups[k].add(name)
    if not groups:
        return na(ctx, "No power nets identified; not applicable.")

    aliases = [sorted(v) for v in groups.values() if len(v) > 1]
    flagged = bool(aliases)
    msg = ("Power nets that look like one rail split by naming: "
           + "; ".join("=".join(a) for a in aliases[:6]) + ".") if flagged \
        else "No aliased power-rail names."
    return advisory(ctx, flagged, count_metric(len(aliases)), msg)
