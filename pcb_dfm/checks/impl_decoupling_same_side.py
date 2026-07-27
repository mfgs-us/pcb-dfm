"""Decoupling cap on the same side as the IC it serves (loop inductance)."""

from __future__ import annotations

from math import hypot

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..ingest.design_intel import is_decoupling_candidate
from ..results import CheckResult
from ._design_advisory import advisory, count_metric, na
from ._design_review import resolve_design


@register_check("decoupling_same_side")
def run_decoupling_same_side(ctx: CheckContext) -> CheckResult:
    r = resolve_design(ctx.design_data)
    if r is None:
        return na(ctx, "No netlist + BOM to resolve decoupling; not applicable.")
    max_lat = float((ctx.check_def.raw.get("params", {}) or {}).get("max_lateral_mm", 3.0))

    bad = []
    for cap_ref in r.refs_of_class("capacitor"):
        cap = r.comp_by_ref[cap_ref]
        if not is_decoupling_candidate(cap) or cap.x_mm is None or not cap.side:
            continue
        # Nearest IC power-pin the cap could serve: an IC access point on one of
        # the cap's power nets. Measuring to the pin (not the IC centroid) keeps a
        # backside-under-pin cap -- which is good practice -- from being flagged.
        best = None  # (distance, ic_side, ic_ref)
        for pn in (n for n in r.nets_of(cap_ref) if r.net_func.get(n) == "power"):
            for pt in r.dd.nets[pn].points:
                if r.part_class.get(pt.ref or "") != "ic":
                    continue
                ic = r.comp_by_ref.get(pt.ref or "")
                if ic is None or not ic.side:
                    continue
                d = hypot(cap.x_mm - pt.x_mm, cap.y_mm - pt.y_mm)
                if best is None or d < best[0]:
                    best = (d, ic.side, pt.ref)
        if best is not None and best[1] != cap.side and best[0] > max_lat:
            bad.append(f"{cap_ref}->{best[2]} ({best[0]:.1f} mm, opposite side)")
    bad.sort()
    flagged = bool(bad)
    msg = (f"Decoupling cap(s) opposite-side and > {max_lat:.0f} mm from their IC pin: "
           f"{', '.join(bad[:6])} -> long inductive loop.") if flagged \
        else "Decoupling caps are on the IC side or close under-pin."
    return advisory(ctx, flagged, count_metric(len(bad)), msg)
