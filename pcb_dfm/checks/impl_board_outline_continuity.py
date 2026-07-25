"""Board outline continuity -- is the routed profile a closed contour?

An outline (Edge.Cuts) that does not close is one of the most common hard fab
rejects: the fabricator cannot determine the board boundary, so it cannot rout
the board. A designer leaves a corner unjoined, or a gap between two segments,
and the package is unmanufacturable.

This is a definitive, exact check, not a heuristic. The board boundary exists
when the outline segments chain into at least one closed loop (reusing the same
contour assembly the copper-to-edge check uses). It does NOT fail on stray
dangling ends from dimension lines or plot marks -- those are common and
harmless, and a board with a closed boundary plus a stray line still passes
(the lesson from the copper-to-edge false positive, #18). It fails only when no
closed boundary forms at all.
"""

from __future__ import annotations

from math import hypot, inf
from typing import List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.gerber_backend import (
    GERBONARA_AVAILABLE,
    outline_contours_mm,
    outline_open_endpoints_mm,
)
from ..results import CheckResult, MetricResult, Violation, ViolationLocation


def _limits(ctx: CheckContext) -> Tuple[float, float]:
    metric_cfg = ctx.check_def.metric or {}
    target_cfg = metric_cfg.get("target", {}) or {}
    limits_cfg = metric_cfg.get("limits", {}) or {}
    target_max = float(target_cfg.get("max", 0.0))
    limit_max = float(limits_cfg.get("max", 0.05))
    return target_max, limit_max


def _na(ctx: CheckContext, msg: str) -> CheckResult:
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="not_applicable",
        severity="info",
        score=None,
        metric=None,
        violations=[Violation(severity="info", message=msg, location=None)],
    ).finalize()


def _smallest_closing_gap(endpoints: List[Tuple[float, float]]) -> Tuple[float, Optional[Tuple[float, float]]]:
    """The smallest distance between two dangling ends -- the gap to close, and
    its midpoint for the violation marker."""
    best = inf
    loc: Optional[Tuple[float, float]] = None
    for i, a in enumerate(endpoints):
        for b in endpoints[i + 1:]:
            d = hypot(a[0] - b[0], a[1] - b[1])
            if d < best:
                best = d
                loc = (0.5 * (a[0] + b[0]), 0.5 * (a[1] + b[1]))
    return best, loc


@register_check("board_outline_continuity")
def run_board_outline_continuity(ctx: CheckContext) -> CheckResult:
    target_max, limit_max = _limits(ctx)

    if not GERBONARA_AVAILABLE:
        return _na(ctx, "Gerber parser unavailable; cannot evaluate outline continuity.")

    outline_files = [f for f in ctx.ingest.files if getattr(f, "layer_type", None) == "outline"]
    if not outline_files:
        # No Edge.Cuts in the package. Some fabs receive the profile separately,
        # so this is a data gap, not a defect.
        return _na(ctx, "No board outline layer present; outline continuity not evaluable.")

    contours = []
    open_endpoints: List[Tuple[float, float]] = []
    for f in outline_files:
        contours.extend(outline_contours_mm(f.path))
        open_endpoints.extend(outline_open_endpoints_mm(f.path))

    has_outline_geometry = bool(contours or open_endpoints)
    if not has_outline_geometry:
        return _na(ctx, "Outline layer present but carries no geometry; not evaluable.")

    # A closed boundary exists -> the board is routable. Stray dangling ends
    # (dimension lines, plot marks) are common and do not change that.
    if contours:
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="pass",
            severity="info",
            score=100.0,
            metric=MetricResult.geometry_mm(
                measured_mm=0.0, target_mm=target_max, limit_low_mm=None,
            ),
            violations=[Violation(
                severity="info",
                message="Board outline forms a closed contour.",
                location=None,
            )],
        ).finalize()

    # Outline geometry exists but nothing closes -> the profile is open.
    gap, loc = _smallest_closing_gap(open_endpoints)
    gap = 0.0 if gap is inf else gap

    # A gap within the fab's join tolerance may be auto-closed; a larger one is a
    # definite reject.
    status = "warning" if 0.0 < gap <= limit_max else "fail"
    severity = "warning" if status == "warning" else "error"

    where = f" (nearest gap ~{gap:.2f} mm)" if gap > 0.0 else ""
    location = None
    if loc is not None:
        location = ViolationLocation(
            layer="Outline", x_mm=loc[0], y_mm=loc[1],
            notes="Board outline does not close here.",
        )
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity=severity,
        score=0.0 if status == "fail" else 60.0,
        metric=MetricResult.geometry_mm(
            measured_mm=float(gap), target_mm=target_max, limit_low_mm=None,
        ),
        violations=[Violation(
            severity=severity,
            message=(
                f"Board outline is not a closed contour{where}; the fabricator "
                f"cannot determine the board boundary. Join the outline into a "
                f"single closed loop."
            ),
            location=location,
        )],
    ).finalize()
