"""Design advisory: fine-pitch parts want a local fiducial pair.

Global fiducials (checked by ``fiducial_coverage``) align the whole board, but
the finest-pitch parts -- BGAs and <=0.5 mm QFN/connectors -- need fiducials
*near them* so the placement machine can correct local placement error the global
fiducials cannot. A fine-pitch part with no fiducial pair within reach is a
placement-yield risk on a larger board.

Needs component placement and at least one fiducial in the design data; otherwise
not_applicable. Conservative: only genuinely fine-pitch multi-pad parts count,
and the local radius is generous so a small board with corner fiducials passes.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..ingest.design_intel import classify_component
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, count_metric, na

_FINE_PITCH_MM = 0.5       # BGA / fine-pitch QFN threshold
_MIN_PADS = 5              # a real fine-pitch part, not a 2-terminal passive
_LOCAL_RADIUS_MM = 40.0    # a fiducial within this reach counts as "local"
_WANT_LOCAL = 2            # placement wants a pair to resolve x/y + rotation


def _centroid(pads) -> Tuple[float, float]:
    return (sum(p.x_mm for p in pads) / len(pads),
            sum(p.y_mm for p in pads) / len(pads))


def _min_pitch(pads) -> Optional[float]:
    n = len(pads)
    if n < 2:
        return None
    best = float("inf")
    for i in range(n):
        xi, yi = pads[i].x_mm, pads[i].y_mm
        for j in range(i + 1, n):
            d2 = (pads[j].x_mm - xi) ** 2 + (pads[j].y_mm - yi) ** 2
            if d2 < best:
                best = d2
    return best ** 0.5 if best < float("inf") else None


def _fiducial_xy(comp) -> Optional[Tuple[float, float]]:
    if comp.pads:
        return _centroid(comp.pads)
    if comp.x_mm is not None and comp.y_mm is not None:
        return (comp.x_mm, comp.y_mm)
    return None


@register_check("fine_pitch_fiducials")
def run_fine_pitch_fiducials(ctx: CheckContext) -> CheckResult:
    dd = getattr(ctx, "design_data", None)
    comps = [c for c in (getattr(dd, "components", None) or [])
             if getattr(c, "placed", True)]
    if not comps:
        return na(ctx, "Needs component placement to evaluate fiducial locality.")

    fiducials = [xy for c in comps if classify_component(c)[0] == "fiducial"
                 for xy in [_fiducial_xy(c)] if xy is not None]
    if not fiducials:
        return na(ctx, "No fiducials in the design data; cannot assess local coverage.")

    fine_pitch: List[Tuple[str, Tuple[float, float]]] = []
    for c in comps:
        if len(c.pads) < _MIN_PADS:
            continue
        pitch = _min_pitch(c.pads)
        if pitch is not None and pitch <= _FINE_PITCH_MM + 1e-9:
            fine_pitch.append((c.ref, _centroid(c.pads)))
    if not fine_pitch:
        return na(ctx, f"No fine-pitch (<= {_FINE_PITCH_MM} mm) parts to evaluate.")

    r2 = _LOCAL_RADIUS_MM ** 2
    under: List[Tuple[str, float, float]] = []
    for ref, (cx, cy) in fine_pitch:
        local = sum(1 for (fx, fy) in fiducials
                    if (fx - cx) ** 2 + (fy - cy) ** 2 <= r2)
        if local < _WANT_LOCAL:
            under.append((ref, cx, cy))

    count = len(under)
    if count == 0:
        return advisory(ctx, False, count_metric(0),
                        f"All {len(fine_pitch)} fine-pitch part(s) have >= {_WANT_LOCAL} "
                        f"fiducial(s) within {_LOCAL_RADIUS_MM:.0f} mm.")
    ref, x, y = under[0]
    return advisory(
        ctx, True, count_metric(count),
        f"{count} fine-pitch part(s) have fewer than {_WANT_LOCAL} fiducials within "
        f"{_LOCAL_RADIUS_MM:.0f} mm (e.g. {ref}) -- add a local fiducial pair so "
        f"placement can correct local error.",
        ViolationLocation(layer="placement", x_mm=x, y_mm=y,
                          notes=f"{ref} lacks a local fiducial pair."),
    )
