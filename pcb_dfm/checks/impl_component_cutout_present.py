"""The artwork is missing a cutout the design data declares.

A mid-mount USB-C, an SD-card holder, a buzzer or a recessed connector needs a
milled opening, and in KiCad the footprint states that requirement by drawing the
opening on ``Edge.Cuts`` inside its own graphics.

**Scope -- read this before extending the check.** KiCad plots a footprint's
Edge.Cuts graphics straight into the board outline layer. Verified empirically:
adding an Edge.Cuts rect inside a footprint takes the plotted outline from one
closed contour to two, the second at exactly the footprint's position. There is no
"propagate the footprint cutout to the board outline" step for a designer to
forget, so when the design data and the artwork come from the same board file --
which is what this engine does by default on a KiCad input -- this check passes
trivially and always.

What it therefore catches is a **design-data vs artwork mismatch**: a fab package
that predates the board file it is paired with. Old Gerbers plus an updated board
declaring a new opening is a real and expensive failure (the fab mills the old
outline), and it is invisible to every other check because both halves are
internally valid.

What it does NOT catch, and cannot: a footprint that *should* declare an opening
and does not. A reverse-mount LED whose library footprint never drew its light
window is a part-knowledge fact, not a geometric one -- see
``docs/cutout_check_domains.md`` §4/§5. Nothing here guesses that "a part named
like a mid-mount USB-C probably needs a cutout"; being wrong about that claims
missing milling on a board that is missing none.

Deliberately one-directional: a board cutout that no component asked for is NOT a
finding. Ventilation, mechanical clearance, mounting and antenna keep-outs are all
legitimate reasons for an opening nothing declares.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.gerber_backend import GERBONARA_AVAILABLE
from ..results import CheckResult, Violation, ViolationLocation
from ._design_advisory import (
    _poly_area,
    count_metric,
    na,
    outline_contours,
    point_inside,
)


def _centroid(poly: List[Tuple[float, float]]) -> Tuple[float, float]:
    n = len(poly)
    return (sum(p[0] for p in poly) / n, sum(p[1] for p in poly) / n)


def _matches(required: List[Tuple[float, float]],
             cutouts: List[List[Tuple[float, float]]],
             area_tol: float) -> bool:
    """Is there a milled cutout that plausibly IS this declared opening?

    Centroid containment is the primary test -- an opening milled slightly larger
    or with rounded router corners is still the right opening, and demanding shape
    equality would fail every real board. The area ratio is a sanity bound so a
    part sitting inside one huge unrelated void does not count as satisfied.
    """
    cx, cy = _centroid(required)
    want = _poly_area(required)
    for cut in cutouts:
        if not point_inside(cut, cx, cy):
            continue
        got = _poly_area(cut)
        if want <= 0 or got <= 0:
            return True
        ratio = got / want
        if (1.0 / area_tol) <= ratio <= area_tol:
            return True
    return False


@register_check("component_cutout_present")
def run_component_cutout_present(ctx: CheckContext) -> CheckResult:
    p = (ctx.check_def.raw or {}).get("params", {}) or {}
    area_tol = float(p.get("area_ratio_tolerance", 6.0))

    dd = ctx.design_data
    if dd is None or not dd.components:
        return na(ctx, "No component data; cutout requirements are not recoverable "
                       "from artwork alone.")

    declaring = [c for c in dd.components if getattr(c, "required_cutouts", None)]
    if not declaring:
        return na(
            ctx,
            "No footprint declares a board cutout (Edge.Cuts graphics inside a "
            "footprint), so there is no cutout requirement to verify.",
        )
    if not GERBONARA_AVAILABLE:
        return na(ctx, "Gerber parser unavailable; cannot read the board outline.")

    boundary, cutouts = outline_contours(ctx)
    if boundary is None:
        return na(ctx, "No board outline to compare declared cutouts against.")

    missing: List[Tuple[str, float, float]] = []
    satisfied = 0
    for comp in declaring:
        for required in comp.required_cutouts:
            if len(required) < 3:
                continue
            if _matches(required, cutouts, area_tol):
                satisfied += 1
            else:
                cx, cy = _centroid(required)
                missing.append((comp.ref or "?", cx, cy))

    if not missing:
        return _result(
            ctx, [], count_metric(0),
            f"Every footprint-declared cutout is present in the board outline "
            f"({satisfied} opening(s) across {len(declaring)} component(s)).",
            None,
        )

    missing.sort()
    refs = ", ".join(f"{ref} at ({x:.1f}, {y:.1f})" for ref, x, y in missing[:6])
    msg = (
        f"{len(missing)} footprint-declared board cutout(s) are missing from the "
        f"outline: {refs}. The footprint draws the opening it needs on Edge.Cuts, "
        f"but the board outline has no matching cutout there -- the milling was "
        f"not propagated, so the part cannot seat."
    )
    loc = ViolationLocation(layer=None, x_mm=missing[0][1], y_mm=missing[0][2],
                            notes=f"{missing[0][0]} declares a cutout that is not milled.")
    return _result(ctx, missing, count_metric(len(missing)), msg, loc)


def _result(ctx: CheckContext, missing, metric, message: str,
            loc: Optional[ViolationLocation]) -> CheckResult:
    flagged = bool(missing)
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="fail" if flagged else "pass",
        severity="info",  # finalize() promotes from the violation
        score=0.0 if flagged else 100.0,
        metric=metric,
        violations=[Violation(
            severity=ctx.check_def.severity if flagged else "info",
            message=message,
            location=loc if flagged else None,
        )],
    ).finalize()
