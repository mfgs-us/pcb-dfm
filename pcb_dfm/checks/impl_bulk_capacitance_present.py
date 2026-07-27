"""Bulk capacitance present -- a board that powers ICs needs bulk energy storage."""

from __future__ import annotations

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult
from ._design_advisory import advisory, count_metric, na
from ._design_review import cap_farads, resolve_design

_BULK_FARADS = 1e-6  # >= 1 uF counts as bulk


@register_check("bulk_capacitance_present")
def run_bulk_capacitance_present(ctx: CheckContext) -> CheckResult:
    r = resolve_design(ctx.design_data)
    if r is None:
        return na(ctx, "No netlist + BOM to resolve rails; not applicable.")
    if not r.ic_supply_rails():
        return na(ctx, "No IC supply rails; bulk capacitance not applicable.")
    if not r.ground_nets():
        return na(ctx, "No ground net identified; cannot review bulk capacitance.")

    caps = r.refs_of_class("capacitor")
    valued = [(ref, cap_farads(r.comp_by_ref[ref])) for ref in caps]
    if not any(f is not None for _ref, f in valued):
        return na(ctx, "No capacitor values in the BOM; cannot assess bulk capacitance.")

    has_bulk = False
    for ref, farads in valued:
        if farads is None or farads < _BULK_FARADS:
            continue
        nets = r.nets_of(ref)
        if any(r.net_func.get(n) == "power" for n in nets) and \
           any(r.net_func.get(n) == "ground" for n in nets):
            has_bulk = True
            break
    flagged = not has_bulk
    msg = ("No bulk capacitor (>= 1 uF) bridges any supply rail to ground -> no "
           "energy storage for load transients.") if flagged \
        else "Bulk capacitance present on the supply."
    return advisory(ctx, flagged, count_metric(1 if flagged else 0), msg)
