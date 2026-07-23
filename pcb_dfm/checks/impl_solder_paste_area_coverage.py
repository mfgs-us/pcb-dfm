from __future__ import annotations

from math import hypot
from pathlib import Path
from typing import List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.gerber_backend import GERBONARA_AVAILABLE, gerber_flash_polygons_mm
from ..results import CheckResult, MetricResult, Violation, ViolationLocation


def _range(ctx: CheckContext):
    metric_cfg = ctx.check_def.metric or {}
    target_cfg = metric_cfg.get("target", {}) or {}
    limits_cfg = metric_cfg.get("limits", {}) or {}
    target_min = float(target_cfg.get("min", 50.0))
    target_max = float(target_cfg.get("max", 120.0))
    limit_min = float(limits_cfg.get("min", 30.0))
    limit_max = float(limits_cfg.get("max", 150.0))
    return target_min, target_max, limit_min, limit_max


def _severity(ctx: CheckContext) -> str:
    return (
        ctx.check_def.raw.get("severity_default")
        or ctx.check_def.severity
        or "warning"
    )


def _collect_pads(path) -> List[Tuple[float, float, float]]:
    """Flashed pads as (area_mm2, x_mm, y_mm), via the gerbonara backend (#3).

    Uses the flash outlines' true filled geometry, so obround/polygon/macro
    apertures get a real area instead of the per-shape approximations the
    pcb-tools primitive path needed.
    """
    pads: List[Tuple[float, float, float]] = []
    for poly in gerber_flash_polygons_mm(Path(path)):
        pts = [(v.x, v.y) for v in poly.vertices]
        if len(pts) < 3:
            continue
        area = 0.0
        for k in range(len(pts)):
            x1, y1 = pts[k]
            x2, y2 = pts[(k + 1) % len(pts)]
            area += x1 * y2 - x2 * y1
        area = abs(area) * 0.5
        if area <= 0.0:
            continue
        b = poly.bounds()
        pads.append((area, 0.5 * (b.min_x + b.max_x), 0.5 * (b.min_y + b.max_y)))
    return pads


def _na(ctx, target_min, limit_min, limit_max, msg) -> CheckResult:
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="not_applicable",
        severity="info",
        score=None,
        metric=MetricResult(
            kind="ratio", units="%", measured_value=None,
            target=target_min, limit_low=limit_min, limit_high=limit_max,
        ),
        violations=[Violation(severity="info", message=msg, location=None)],
    ).finalize()


@register_check("solder_paste_area_coverage")
def run_solder_paste_area_coverage(ctx: CheckContext) -> CheckResult:
    """Ratio of solder-paste aperture area to the underlying copper pad area.

    Heuristic (artwork-only):
      - Identify paste layers (.gtp/.gbp, or names containing 'paste').
      - For each paste aperture (flashed pad), match it to the nearest flashed
        copper pad on the same side and compute coverage = paste/copper * 100%.
      - Report the mean coverage; grade it against the target/limit range.
    If there is no paste layer, or paste apertures cannot be matched to copper
    pads, the check is not applicable rather than fabricated.
    """
    target_min, target_max, limit_min, limit_max = _range(ctx)

    if not GERBONARA_AVAILABLE:
        return _na(ctx, target_min, limit_min, limit_max,
                   "Gerber parser unavailable; cannot measure paste coverage.")

    # Locate paste files. Ingest classifies paste as layer_type 'other', so we
    # identify them by extension / name.
    paste_files = [
        f for f in ctx.ingest.files
        if f.extension in (".gtp", ".gbp") or "paste" in f.original_name.lower()
    ]
    if not paste_files:
        return _na(ctx, target_min, limit_min, limit_max,
                   "No solder paste layer (.gtp/.gbp) present; paste coverage not applicable.")

    match_tol = float((ctx.check_def.raw or {}).get("pad_match_tolerance_mm", 0.6))

    coverages: List[float] = []
    # Track the extreme pads (lowest and highest coverage) so a single
    # starved or flooded pad drives the grade, instead of being averaged away.
    lo_pad: Optional[Tuple[float, float, float]] = None  # (coverage, x, y)
    hi_pad: Optional[Tuple[float, float, float]] = None  # (coverage, x, y)

    for pf in paste_files:
        # Copper on the same side.
        copper_files = [
            f for f in ctx.ingest.files
            if f.layer_type == "copper" and f.side == pf.side
        ]
        if not copper_files:
            continue
        paste_pads = _collect_pads(pf.path)
        copper_pads: List[Tuple[float, float, float]] = []
        for cf in copper_files:
            copper_pads.extend(_collect_pads(cf.path))
        if not paste_pads or not copper_pads:
            continue

        for (parea, px, py) in paste_pads:
            # Nearest copper pad by centroid.
            best = None
            best_d = None
            for (carea, cx, cy) in copper_pads:
                d = hypot(px - cx, py - cy)
                if best_d is None or d < best_d:
                    best_d = d
                    best = (carea, cx, cy)
            if best is None or best_d is None or best_d > match_tol:
                continue
            carea = best[0]
            if carea <= 0.0:
                continue
            cov = 100.0 * parea / carea
            coverages.append(cov)
            if lo_pad is None or cov < lo_pad[0]:
                lo_pad = (cov, px, py)
            if hi_pad is None or cov > hi_pad[0]:
                hi_pad = (cov, px, py)

    if not coverages:
        return _na(ctx, target_min, limit_min, limit_max,
                   "Paste layer present but no paste apertures could be matched to copper pads; not applicable.")

    mean_cov = sum(coverages) / len(coverages)
    lo_cov = lo_pad[0] if lo_pad is not None else mean_cov
    hi_cov = hi_pad[0] if hi_pad is not None else mean_cov

    def _score_cov(c: float) -> float:
        if target_min <= c <= target_max:
            return 100.0
        if c < target_min:
            span = max(1e-9, target_min - limit_min)
            return max(0.0, min(100.0, 100.0 * (c - limit_min) / span))
        span = max(1e-9, limit_max - target_max)
        return max(0.0, min(100.0, 100.0 * (limit_max - c) / span))

    # Grade off the WORST-deviating pad on each side, not the mean: a single
    # starved (low) or flooded (high) pad must be able to warn/fail even when
    # the average sits comfortably in range.
    score_lo = _score_cov(lo_cov)
    score_hi = _score_cov(hi_cov)
    if score_lo <= score_hi:
        measured, worst = lo_cov, lo_pad
    else:
        measured, worst = hi_cov, hi_pad
    score = min(score_lo, score_hi)

    # Two-sided range grading driven by the extreme pads.
    if lo_cov < limit_min or hi_cov > limit_max:
        status = "fail"
    elif lo_cov < target_min or hi_cov > target_max:
        status = "warning"
    else:
        status = "pass"

    # margin to the nearer limit for the worst pad (positive = inside limits)
    margin = min(measured - limit_min, limit_max - measured)

    violations: List[Violation] = []
    if status != "pass":
        loc = None
        if worst is not None:
            loc = ViolationLocation(
                layer="Paste", x_mm=worst[1], y_mm=worst[2],
                notes="Pad with paste coverage furthest outside the recommended range.",
            )
        violations.append(Violation(
            severity=_severity(ctx),
            message=(
                f"Worst solder-paste coverage {measured:.1f}% "
                f"(pad range {lo_cov:.1f}-{hi_cov:.1f}%, mean {mean_cov:.1f}%, "
                f"{len(coverages)} pad(s)) is outside the recommended "
                f"{target_min:.0f}-{target_max:.0f}% (allowed {limit_min:.0f}-{limit_max:.0f}%)."
            ),
            location=loc,
        ))

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",
        score=score,
        metric=MetricResult(
            kind="ratio", units="%",
            measured_value=float(measured),
            target=target_min,
            limit_low=limit_min,
            limit_high=limit_max,
            margin_to_limit=float(margin),
        ),
        violations=violations,
    ).finalize()
