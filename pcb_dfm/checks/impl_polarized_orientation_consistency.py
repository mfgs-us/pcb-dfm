"""Polarized part orientation outlier within a same-footprint group."""

from __future__ import annotations

from collections import Counter, defaultdict

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..ingest.design_intel import classify_component
from ..results import CheckResult
from ._design_advisory import advisory, count_metric, na


@register_check("polarized_orientation_consistency")
def run_polarized_orientation_consistency(ctx: CheckContext) -> CheckResult:
    dd = ctx.design_data
    if dd is None or not dd.components:
        return na(ctx, "No component placement; not applicable.")
    params = ctx.check_def.raw.get("params", {}) or {}
    min_group = int(params.get("min_group", 4))
    maj_frac = float(params.get("majority_frac", 0.75))

    groups = defaultdict(list)
    for c in dd.components:
        _cls, polarized = classify_component(c)
        if not polarized or not c.footprint or c.x_mm is None:
            continue
        groups[(c.value, c.footprint)].append(c)
    if not any(len(m) >= min_group for m in groups.values()):
        return na(ctx, "No group of >= 4 identical polarized parts to compare; not applicable.")

    outliers = []
    for members in groups.values():
        if len(members) < min_group:
            continue
        angles = Counter(round(m.rotation_deg) % 360 for m in members)
        maj_angle, maj_count = angles.most_common(1)[0]
        if maj_count / len(members) < maj_frac:
            continue  # no strong majority -> orientation is genuinely mixed
        for m in members:
            if round(m.rotation_deg) % 360 != maj_angle:
                outliers.append(f"{m.ref} @{round(m.rotation_deg) % 360}deg")
    outliers.sort()
    flagged = bool(outliers)
    msg = (f"Polarized part(s) at an odd angle vs their group majority: "
           f"{', '.join(outliers[:8])} -- verify orientation.") if flagged \
        else "Polarized parts are consistently oriented within their groups."
    return advisory(ctx, flagged, count_metric(len(outliers)), msg)
