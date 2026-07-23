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

    # Silk lying ENTIRELY outside the board is fab documentation -- title
    # blocks, layer-identification text, fab notes -- not board silkscreen. It
    # is not printed on the board and cannot be "too close" to anything on it.
    # Counting it made every real board fail: eagle_gyw has a title block 6.3 mm
    # past the right edge, which was reported as -6.32 mm of clearance (#19).
    silk_bboxes = [
        bb for bb in silk_bboxes
        if not (bb[0] > board.max_x or bb[1] < board.min_x
                or bb[2] > board.max_y or bb[3] < board.min_y)
    ]
    if not silk_bboxes:
        return _na(ctx, rec, ab,
                   "All silkscreen features lie outside the board outline (fab "
                   "documentation); no on-board silkscreen to evaluate.")

    drills = _collect_drills(ctx)
    holes = [(h.x_mm, h.y_mm, 0.5 * h.diameter_mm) for h in drills if h.diameter_mm > 0.0]

    min_clear: Optional[float] = None
    # Graded separately: silk running off the board edge is trimmed at
    # fabrication (cosmetic), while silk over a drilled hole is smeared by the
    # drill and is a real defect. See the grading below.
    min_hole_clear: Optional[float] = None
    min_edge_clear: Optional[float] = None
    worst_location: Optional[ViolationLocation] = None
    worst_kind = ""
    # (clearance_mm, kind, x_mm, y_mm)
    offenders: List[Tuple[float, str, float, float]] = []

    for bb in silk_bboxes:
        min_x, max_x, min_y, max_y = bb
        cx, cy = 0.5 * (min_x + max_x), 0.5 * (min_y + max_y)

        edge_clr = _edge_clearance(bb, board)
        clr = edge_clr
        kind = "board edge"

        for hx, hy, r in holes:
            # bbox gap to hole centre is a cheap lower bound on hole clearance;
            # only refine when it could beat the running best for this feature.
            gap = _hole_clearance(bb, hx, hy, r)
            if gap < clr:
                clr = gap
                kind = "drilled hole"
            if min_hole_clear is None or gap < min_hole_clear:
                min_hole_clear = gap

        if min_edge_clear is None or edge_clr < min_edge_clear:
            min_edge_clear = edge_clr

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

    # Silk over a drilled HOLE is a genuine defect: the drill smears the ink and
    # can leave it in the barrel, so it grades normally and can fail.
    #
    # Silk past the board EDGE is different in kind. It is routed away when the
    # board is cut out, so the printed board is unaffected; it is also very
    # commonly just an annotation or leader line drawn on the silk layer. It
    # therefore never fails on its own -- it is capped at a warning. Grading the
    # two together failed every real board in the corpus on what is, for two of
    # them, a cosmetic overhang (#19).
    def _grade(v: Optional[float], can_fail: bool) -> str:
        if v is None:
            return "pass"
        if v < ab:
            return "fail" if can_fail else "warning"
        if v < rec:
            return "warning"
        return "pass"

    _ORDER = {"pass": 0, "warning": 1, "fail": 2}
    status = max(_grade(min_hole_clear, True), _grade(min_edge_clear, False),
                 key=lambda st: _ORDER[st])

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
        # Hole overlaps first: those are what can fail the board, so they must
        # lead the list even when a (cosmetic) edge overhang is numerically worse.
        offenders.sort(key=lambda t: (t[1] != "drilled hole", t[0]))
        for clr, kind, x_mm, y_mm in offenders[:MAX_REPORTED_VIOLATIONS]:
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
