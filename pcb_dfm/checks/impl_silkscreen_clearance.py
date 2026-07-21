from __future__ import annotations

import os
from math import hypot
from typing import List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry import queries
from ..results import CheckResult, MetricResult, Violation, ViolationLocation
from .impl_drill_to_drill_spacing import _collect_drills
from .impl_silkscreen_on_copper import _cached_silk_bboxes

MAX_REPORTED_VIOLATIONS = 100


def _thresholds(ctx: CheckContext) -> Tuple[float, float]:
    """Clearance thresholds in mm (plumbed from the µm metric)."""
    limits = ctx.check_def.limits or {}
    recommended_min = float(limits.get("recommended_min", 0.15))  # 150 µm
    absolute_min = float(limits.get("absolute_min", 0.10))        # 100 µm
    return recommended_min, absolute_min


def _na(ctx: CheckContext, rec: float, ab: float, msg: str) -> CheckResult:
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="not_applicable",
        severity="info",
        score=None,
        metric=MetricResult.geometry_mm(None, target_mm=rec, limit_low_mm=ab),
        violations=[Violation(severity="info", message=msg, location=None)],
    ).finalize()


def _edge_clearance(bb, board) -> float:
    """Signed clearance from a silk bbox to the board bounding box.

    Positive = silk sits inside the board with this much margin to the nearest
    edge; negative = silk pokes past the routed outline (printed on the rail /
    milled away). Uses the board bounding box, which is exact for the common
    rectangular outline and a close bound for others.
    """
    min_x, max_x, min_y, max_y = bb
    left = min_x - board.min_x
    right = board.max_x - max_x
    bottom = min_y - board.min_y
    top = board.max_y - max_y
    return min(left, right, bottom, top)


def _hole_clearance(bb, hx: float, hy: float, r: float) -> float:
    """Clearance from a silk bbox to a drilled hole rim (negative if silk
    overlaps the hole)."""
    min_x, max_x, min_y, max_y = bb
    dx = max(min_x - hx, 0.0, hx - max_x)
    dy = max(min_y - hy, 0.0, hy - max_y)
    return hypot(dx, dy) - r


@register_check("silkscreen_clearance")
def run_silkscreen_clearance(ctx: CheckContext) -> CheckResult:
    """Silkscreen clearance to the board edge and to drilled holes.

    Silk that runs off the routed outline prints on the rail (or is milled
    away), and silk over a drilled hole is partly drilled off and can smear —
    both are common, avoidable print-quality defects. For every silkscreen
    feature we measure the smaller of its clearance to the board edge and its
    clearance to the nearest hole rim, and report the worst case across the
    board. Silk features are taken as their primitive bounding boxes (a
    conservative envelope of stroke/text extent); holes and the outline are
    real geometry.
    """
    rec, ab = _thresholds(ctx)

    board = queries.get_board_bounds(ctx.geometry)
    if board is None:
        return _na(ctx, rec, ab, "No board outline available to evaluate silkscreen clearance.")

    silk_files = [f for f in ctx.ingest.files if f.layer_type in ("silk", "silkscreen")]
    if not silk_files:
        return _na(ctx, rec, ab, "No silkscreen layers present; clearance not applicable.")

    silk_bboxes: List[Tuple[float, float, float, float]] = []
    for f in silk_files:
        try:
            mtime_ns = os.stat(str(f.path)).st_mtime_ns
        except OSError:
            continue
        silk_bboxes.extend(_cached_silk_bboxes(str(f.path), mtime_ns))

    if not silk_bboxes:
        return _na(ctx, rec, ab, "No silkscreen features found to evaluate clearance.")

    drills = _collect_drills(ctx)
    holes = [(h.x_mm, h.y_mm, 0.5 * h.diameter_mm) for h in drills if h.diameter_mm > 0.0]

    min_clear: Optional[float] = None
    worst_location: Optional[ViolationLocation] = None
    worst_kind = ""
    # (clearance_mm, kind, x_mm, y_mm)
    offenders: List[Tuple[float, str, float, float]] = []

    for bb in silk_bboxes:
        min_x, max_x, min_y, max_y = bb
        cx, cy = 0.5 * (min_x + max_x), 0.5 * (min_y + max_y)

        clr = _edge_clearance(bb, board)
        kind = "board edge"

        for hx, hy, r in holes:
            # bbox gap to hole centre is a cheap lower bound on hole clearance;
            # only refine when it could beat the running best for this feature.
            gap = _hole_clearance(bb, hx, hy, r)
            if gap < clr:
                clr = gap
                kind = "drilled hole"

        if min_clear is None or clr < min_clear:
            min_clear = clr
            worst_kind = kind
            worst_location = ViolationLocation(
                layer="Silkscreen",
                x_mm=cx,
                y_mm=cy,
                notes=f"Closest silkscreen feature to {kind}.",
            )
        if clr < rec:
            offenders.append((clr, kind, cx, cy))

    assert min_clear is not None

    if min_clear < ab:
        status = "fail"
    elif min_clear < rec:
        status = "warning"
    else:
        status = "pass"

    if min_clear >= rec:
        score = 100.0
    elif min_clear <= ab:
        score = 0.0
    else:
        span = max(1e-9, rec - ab)
        score = max(0.0, min(100.0, 100.0 * (min_clear - ab) / span))

    violations: List[Violation] = []
    if status != "pass":
        severity = "error" if status == "fail" else (ctx.check_def.severity or "warning")
        for clr, kind, x_mm, y_mm in sorted(offenders, key=lambda t: t[0])[:MAX_REPORTED_VIOLATIONS]:
            where = "overlaps" if clr < 0.0 else f"is {clr * 1000:.0f} µm from"
            violations.append(Violation(
                severity=severity,
                message=(
                    f"Silkscreen feature {where} the {kind} "
                    f"(recommended clearance {rec * 1000:.0f} µm, floor {ab * 1000:.0f} µm)."
                ),
                location=ViolationLocation(
                    layer="Silkscreen",
                    x_mm=x_mm,
                    y_mm=y_mm,
                    notes=f"Silkscreen too close to {kind}.",
                ),
            ))
        if not violations:
            violations.append(Violation(
                severity=severity,
                message=(
                    f"Closest silkscreen feature is {min_clear * 1000:.0f} µm from the {worst_kind} "
                    f"(recommended {rec * 1000:.0f} µm, floor {ab * 1000:.0f} µm)."
                ),
                location=worst_location,
            ))

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",
        score=score,
        metric=MetricResult.geometry_mm(
            measured_mm=float(min_clear),
            target_mm=rec,
            limit_low_mm=ab,
        ),
        violations=violations,
    ).finalize()
