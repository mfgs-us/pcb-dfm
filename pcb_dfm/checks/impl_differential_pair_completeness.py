"""Differential pair completeness -- a declared pair missing a member/route."""

from __future__ import annotations

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult
from ._design_advisory import advisory, count_metric, na


@register_check("differential_pair_completeness")
def run_differential_pair_completeness(ctx: CheckContext) -> CheckResult:
    dd = ctx.design_data
    if dd is None or not dd.diff_pairs:
        return na(ctx, "No differential pairs declared; not applicable.")

    bad = []
    for dp in dd.diff_pairs:
        pos = dd.nets.get(dp.positive)
        neg = dd.nets.get(dp.negative)
        issues = []
        if pos is None:
            issues.append(f"{dp.positive} missing")
        if neg is None:
            issues.append(f"{dp.negative} missing")
        if pos is not None and neg is not None and pos.has_geometry() != neg.has_geometry():
            routed = dp.positive if pos.has_geometry() else dp.negative
            unrouted = dp.negative if pos.has_geometry() else dp.positive
            issues.append(f"{unrouted} unrouted while {routed} is routed")
        if issues:
            bad.append(f"{dp.name} ({'; '.join(issues)})")
    flagged = bool(bad)
    msg = (f"Incomplete differential pair(s): {', '.join(bad[:6])}.") if flagged \
        else f"All {len(dd.diff_pairs)} declared differential pair(s) are complete."
    return advisory(ctx, flagged, count_metric(len(bad)), msg)
