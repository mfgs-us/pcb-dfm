"""Design advisory: keep components clear of a mounting hole's mechanical keep-out.

A mounting hole needs a mechanical annulus around it for the screw head, washer,
or standoff. A component placed inside that annulus collides with the hardware at
assembly -- an unambiguous mechanical error, independent of nets (unlike copper,
which is often *deliberately* flooded up to a grounded mounting hole, so copper
proximity is not flagged here).

This is the *mechanical* keep-out (screw-head scale, a few mm), distinct from
``npth_to_copper_clearance`` which is the sub-mm electrical/drill-nick rule.

Needs the artwork's NPTH drills and component placement; otherwise
not_applicable. Only holes large enough to be real mounting holes are considered.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.gerber_backend import GERBONARA_AVAILABLE, excellon_hits_mm
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, count_metric, is_assembly_body, na
from ._design_data_geo import copper_lookup

_MOUNTING_MIN_DIA_MM = 2.0   # smaller NPTH are connector pegs / slots, not mounts
_KEEPOUT_MARGIN_MM = 2.5     # screw-head / standoff annulus beyond the hole edge
_OWNER_TOL_MM = 0.6          # a hole this close to a pad belongs to that footprint


@register_check("mounting_hole_keepout")
def run_mounting_hole_keepout(ctx: CheckContext) -> CheckResult:
    dd = getattr(ctx, "design_data", None)
    all_placed = [c for c in (getattr(dd, "components", None) or [])
                  if getattr(c, "placed", True) and c.pads]
    # Only physical bodies can collide with the hardware; a flat fiducial or a
    # test point near a mounting hole is not a collision.
    comps = [c for c in all_placed if is_assembly_body(c)]
    if not comps:
        return na(ctx, "Needs component placement to evaluate mounting-hole keep-out.")
    if not GERBONARA_AVAILABLE:
        return na(ctx, "Gerber parser unavailable; cannot read NPTH drills.")

    npth_files = [
        f for f in ctx.ingest.files
        if f.layer_type == "drill" and getattr(f, "is_plated", None) is False
    ]
    if not npth_files:
        return na(ctx, "No non-plated (NPTH) drill layer; no mounting holes to evaluate.")

    holes: List[Tuple[float, float, float]] = []  # (cx, cy, radius)
    for info in npth_files:
        for h in excellon_hits_mm(Path(info.path)):
            if h.diameter_mm >= _MOUNTING_MIN_DIA_MM:
                holes.append((h.x_mm, h.y_mm, h.diameter_mm / 2.0))
    if not holes:
        return na(ctx, f"No NPTH >= {_MOUNTING_MIN_DIA_MM:.0f} mm; no mounting holes to evaluate.")

    # Flatten pads to (ref, x, y) once.
    pads = [(c.ref, p.x_mm, p.y_mm) for c in comps for p in c.pads]

    # A registered component pad sits on copper; one floating in empty space
    # means the placement is not aligned to this artwork, and "intrusion" would
    # be a false positive. Judge only against pads that land on copper, and bail
    # out when the placement clearly does not register.
    on_copper = copper_lookup(ctx.geometry)
    on_cu = [(ref, x, y) for (ref, x, y) in pads if on_copper(x, y)]
    if len(on_cu) < max(1, int(0.3 * len(pads))):
        return na(ctx, "Component placement does not register to the copper "
                       "artwork; cannot evaluate mounting-hole keep-out.")
    pads = on_cu

    intrusions: List[Tuple[str, float, float]] = []
    for (hx, hy, r) in holes:
        keepout = r + _KEEPOUT_MARGIN_MM
        # The component that owns the hole (its own mounting/peg pad sits on it)
        # is exempt -- its pads are meant to be there.
        owners = {ref for (ref, px, py) in pads
                  if (px - hx) ** 2 + (py - hy) ** 2 <= (r + _OWNER_TOL_MM) ** 2}
        for (ref, px, py) in pads:
            if ref in owners:
                continue
            d2 = (px - hx) ** 2 + (py - hy) ** 2
            if d2 < keepout ** 2:
                intrusions.append((ref, px, py))

    # De-duplicate: one finding per intruding component.
    seen = set()
    unique: List[Tuple[str, float, float]] = []
    for ref, x, y in intrusions:
        if ref not in seen:
            seen.add(ref)
            unique.append((ref, x, y))

    count = len(unique)
    if count == 0:
        return advisory(ctx, False, count_metric(0),
                        f"All components clear the mounting-hole keep-out "
                        f"({len(holes)} hole(s) evaluated).")
    ref, x, y = unique[0]
    return advisory(
        ctx, True, count_metric(count),
        f"{count} component(s) intrude a mounting hole's mechanical keep-out "
        f"(e.g. {ref}) -- the screw head / standoff will collide at assembly. "
        f"Move the part or the hole.",
        ViolationLocation(layer="placement", x_mm=x, y_mm=y,
                          notes=f"{ref} inside a mounting-hole keep-out."),
    )
