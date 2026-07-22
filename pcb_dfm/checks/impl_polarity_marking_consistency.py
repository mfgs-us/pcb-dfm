"""
polarity_marking_consistency — polarized parts must carry a silkscreen marker.

A polarized component (diode, LED, electrolytic/tantalum cap, IC pin-1,
connector) needs a silkscreen orientation cue so it's assembled the right way
round. This checks, for every polarized *placed, populated* component, that
there is at least one silkscreen feature next to it on the same side.

Requires component identity + placement (BOM merged onto a KiCad project, #6)
and silkscreen artwork. ``not_applicable`` when either is missing.

Scope / honesty: this verifies the **presence** of a nearby marker, not that it
is on the correct pin or points the right way — that needs pin-1 footprint
geometry (a separate ingest). So it catches the common, high-value case of a
polarized part with *no* orientation marking at all, and is labeled heuristic.
"""

from __future__ import annotations

import os
from math import hypot
from typing import Dict, List, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, MetricResult, Violation, ViolationLocation
from .impl_silkscreen_on_copper import _cached_silk_bboxes

Bbox = Tuple[float, float, float, float]


def _norm_side(side) -> str:
    s = str(side or "").lower()
    if "bot" in s or s in ("b", "back"):
        return "bottom"
    return "top"


def _silk_bboxes_by_side(ctx: CheckContext) -> Dict[str, List[Bbox]]:
    out: Dict[str, List[Bbox]] = {"top": [], "bottom": []}
    for f in ctx.ingest.files:
        if f.layer_type not in ("silk", "silkscreen"):
            continue
        try:
            mtime = os.stat(str(f.path)).st_mtime_ns
        except OSError:
            continue
        out[_norm_side(f.side)].extend(_cached_silk_bboxes(str(f.path), mtime))
    return out


def _pt_bbox_dist(px: float, py: float, bb: Bbox) -> float:
    min_x, max_x, min_y, max_y = bb
    dx = max(min_x - px, 0.0, px - max_x)
    dy = max(min_y - py, 0.0, py - max_y)
    return hypot(dx, dy)


def _na(ctx: CheckContext, msg: str) -> CheckResult:
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="not_applicable",
        severity="info",
        score=None,
        metric=MetricResult(kind="boolean", measured_value=None, target=True),
        violations=[Violation(severity="info", message=msg, location=None)],
    ).finalize()


@register_check("polarity_marking_consistency")
def run_polarity_marking_consistency(ctx: CheckContext) -> CheckResult:
    dd = ctx.design_data
    if dd is None or not getattr(dd, "components", None):
        return _na(ctx, "No component identity/placement; provide a design source "
                        "(e.g. a KiCad project) and BOM.")

    raw = ctx.check_def.raw or {}
    radius = float(raw.get("marker_search_radius_mm", 2.5))

    polarized = [
        c for c in dd.components
        if c.polarized and c.placed and not c.dnp
        and c.x_mm is not None and c.y_mm is not None
    ]
    if not polarized:
        return _na(ctx, "No polarized populated components to check.")

    silk = _silk_bboxes_by_side(ctx)
    if not silk["top"] and not silk["bottom"]:
        return _na(ctx, "No silkscreen artwork present to check for polarity markers.")

    unmarked: List = []
    for c in polarized:
        side = _norm_side(c.side)
        boxes = silk.get(side, [])
        if not any(_pt_bbox_dist(c.x_mm, c.y_mm, bb) <= radius for bb in boxes):
            unmarked.append(c)

    total = len(polarized)
    marked = total - len(unmarked)
    ok = not unmarked
    status = "pass" if ok else "warning"
    score = 100.0 * marked / total if total else 100.0

    violations: List[Violation] = []
    if unmarked:
        for c in unmarked[:100]:
            violations.append(Violation(
                severity=ctx.check_def.severity or "warning",
                message=(
                    f"{c.ref} ({c.part_class or 'polarized part'}) has no silkscreen marker "
                    f"within {radius:.1f} mm — add a polarity/pin-1 indicator. "
                    f"Heuristic (marker presence only)."
                ),
                location=ViolationLocation(
                    layer=_norm_side(c.side), x_mm=c.x_mm, y_mm=c.y_mm,
                    component=c.ref, notes="Polarized part missing a silkscreen marker.",
                ),
            ))

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",
        score=score,
        metric=MetricResult(kind="boolean", measured_value=ok, target=True),
        violations=violations,
    ).finalize()
