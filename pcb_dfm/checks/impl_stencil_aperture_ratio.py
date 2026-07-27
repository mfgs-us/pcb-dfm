"""IPC-7525 stencil aperture ratios.

Paste release from a laser-cut stencil is governed by two geometric ratios,
independent of the paste chemistry:

  * **Area ratio** ``AR = aperture_area / aperture_wall_area``
    ``= area / (perimeter * foil_thickness)``. Below ~0.66 the paste's adhesion
    to the aperture walls beats its adhesion to the pad and the print skips or
    smears -> insufficient/​open joints. This is the dominant release predictor
    for fine-pitch openings.
  * **Aspect ratio** ``= min(opening_width, opening_height) / foil_thickness``.
    Below ~1.5 a narrow slot won't release cleanly.

Both depend on the stencil foil thickness. When the design-data carries a
``stencil_thickness_mm`` we treat it as authoritative and a truly un-releasable
aperture (AR < 0.5) can hard-fail; otherwise we assume a standard 0.12 mm
(5 mil) foil and cap the finding at a warning, since a thinner foil could make
the same aperture releasable. Either way we never fabricate a value: with no
paste layer or no measurable apertures the check is not applicable.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.gerber_backend import GERBONARA_AVAILABLE, gerber_flash_polygons_mm
from ..results import CheckResult, MetricResult, Violation, ViolationLocation

# An "aperture" is a paste opening. Anything whose smaller dimension is larger
# than this is a plane-scale window (a windowpaned thermal pad, a paste pour):
# its area ratio is always huge, so it can never fail -- excluding it just keeps
# the reported worst-case honest.
_MAX_APERTURE_DIM_MM = 6.0


def _params(ctx: CheckContext) -> Tuple[float, float, float, float]:
    p = (ctx.check_def.raw or {}).get("params", {}) or {}
    return (
        float(p.get("stencil_thickness_mm", 0.12)),
        float(p.get("min_area_ratio", 0.66)),
        float(p.get("fail_area_ratio", 0.5)),
        float(p.get("min_aspect_ratio", 1.5)),
    )


def _na(ctx: CheckContext, target: float, limit: float, msg: str) -> CheckResult:
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="not_applicable",
        severity="info",
        score=None,
        metric=MetricResult.dimensionless(None, target=target, limit_low=limit),
        violations=[Violation(severity="info", message=msg, location=None)],
    ).finalize()


def _aperture_metrics(poly) -> Optional[Tuple[float, float, float, float]]:
    """(area_ratio_numerator_area, perimeter, min_dim, center packed later).

    Returns ``(area, perimeter, min_dim, _)`` for one aperture polygon, or None
    when it is degenerate. Area is the shoelace area; perimeter the summed edge
    length; min_dim the smaller bounding-box side (the slot width that governs
    the aspect ratio).
    """
    pts = [(v.x, v.y) for v in poly.vertices]
    if len(pts) < 3:
        return None
    area = 0.0
    perim = 0.0
    for k in range(len(pts)):
        x1, y1 = pts[k]
        x2, y2 = pts[(k + 1) % len(pts)]
        area += x1 * y2 - x2 * y1
        perim += ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    area = abs(area) * 0.5
    if area <= 1e-9 or perim <= 1e-9:
        return None
    b = poly.bounds()
    min_dim = min(b.max_x - b.min_x, b.max_y - b.min_y)
    return area, perim, min_dim, 0.0


@register_check("stencil_aperture_ratio")
def run_stencil_aperture_ratio(ctx: CheckContext) -> CheckResult:
    thickness, min_ar, fail_ar, min_aspect = _params(ctx)

    dd = ctx.design_data
    spec_thickness = getattr(dd, "stencil_thickness_mm", None) if dd is not None else None
    authoritative = spec_thickness is not None and spec_thickness > 0.0
    if authoritative:
        thickness = float(spec_thickness)

    if not GERBONARA_AVAILABLE:
        return _na(ctx, min_ar, fail_ar,
                   "Gerber parser unavailable; cannot measure stencil apertures.")

    paste_files = [
        f for f in ctx.ingest.files
        if f.extension in (".gtp", ".gbp") or "paste" in f.original_name.lower()
    ]
    if not paste_files:
        return _na(ctx, min_ar, fail_ar,
                   "No solder-paste layer (.gtp/.gbp) present; stencil aperture ratio not applicable.")

    # Worst (lowest) area ratio drives the grade; also track the worst aspect.
    worst_ar: Optional[float] = None
    worst_ar_loc: Optional[Tuple[float, float, str]] = None  # (x, y, side)
    worst_aspect: Optional[float] = None
    below_ar = 0
    below_aspect = 0
    total = 0

    for pf in paste_files:
        side = pf.side or "top"
        for poly in gerber_flash_polygons_mm(Path(pf.path)):
            m = _aperture_metrics(poly)
            if m is None:
                continue
            area, perim, min_dim, _ = m
            if min_dim > _MAX_APERTURE_DIM_MM:
                continue  # plane-scale window, not a fine-pitch opening
            total += 1
            ar = area / (perim * thickness)
            aspect = min_dim / thickness
            b = poly.bounds()
            cx, cy = 0.5 * (b.min_x + b.max_x), 0.5 * (b.min_y + b.max_y)
            if worst_ar is None or ar < worst_ar:
                worst_ar = ar
                worst_ar_loc = (cx, cy, side)
            if worst_aspect is None or aspect < worst_aspect:
                worst_aspect = aspect
            if ar < min_ar:
                below_ar += 1
            if aspect < min_aspect:
                below_aspect += 1

    if total == 0 or worst_ar is None:
        return _na(ctx, min_ar, fail_ar,
                   "Paste layer present but no measurable apertures; stencil aperture ratio not applicable.")

    assert worst_aspect is not None
    # Grade off the worst area ratio; a failing aspect ratio can only warn.
    if worst_ar < fail_ar:
        status = "fail" if authoritative else "warning"
    elif worst_ar < min_ar or worst_aspect < min_aspect:
        status = "warning"
    else:
        status = "pass"

    # Linear score: limit(fail_ar)=0 -> target(min_ar)=100, clamped.
    span = max(1e-9, min_ar - fail_ar)
    score = max(0.0, min(100.0, 100.0 * (worst_ar - fail_ar) / span))

    violations: List[Violation] = []
    if status != "pass":
        loc = None
        if worst_ar_loc is not None:
            loc = ViolationLocation(
                layer="Paste", x_mm=worst_ar_loc[0], y_mm=worst_ar_loc[1],
                notes="Paste aperture with the lowest IPC-7525 area ratio.",
            )
        assumed = "" if authoritative else " (assumed foil; provide stencil_thickness_mm to confirm)"
        parts = [
            f"Worst stencil aperture ratio {worst_ar:.2f} at {thickness * 1000:.0f} µm foil"
            f"{assumed}: {below_ar} of {total} aperture(s) below the IPC-7525 "
            f"area-ratio floor {min_ar:.2f} (won't release cleanly -> insufficient joints)."
        ]
        if worst_aspect < min_aspect:
            parts.append(
                f" Narrowest opening aspect ratio {worst_aspect:.2f} "
                f"({below_aspect} below {min_aspect:.1f})."
            )
        violations.append(Violation(
            severity=ctx.check_def.raw.get("severity_default", "warning"),
            message="".join(parts),
            location=loc,
        ))

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",
        score=score,
        metric=MetricResult.dimensionless(
            float(worst_ar), target=min_ar, limit_low=fail_ar),
        violations=violations,
    ).finalize()
