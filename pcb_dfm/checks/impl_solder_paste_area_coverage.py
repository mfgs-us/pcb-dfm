from __future__ import annotations

from math import hypot, pi, sin
from typing import List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, MetricResult, Violation, ViolationLocation

try:
    import gerber
    from gerber.primitives import Circle, Ellipse, Obround, Polygon, Rectangle
except Exception:  # pragma: no cover - defensive
    gerber = None
    Circle = Rectangle = Obround = Polygon = Ellipse = None  # type: ignore


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


def _pad_area_and_pos(prim) -> Optional[Tuple[float, float, float]]:
    """Return (area_mm2, x_mm, y_mm) for a flashed pad primitive, else None.

    Only flashed apertures are pads; stroked Line primitives (traces) are
    ignored. Areas use the true shape where known, falling back to the
    bounding box. Assumes the layer has been normalised to mm.
    """
    if not getattr(prim, "flashed", False):
        return None
    try:
        (min_x, max_x), (min_y, max_y) = prim.bounding_box
    except Exception:
        return None
    cx = 0.5 * (float(min_x) + float(max_x))
    cy = 0.5 * (float(min_y) + float(max_y))
    pos = getattr(prim, "position", None)
    if pos is not None:
        try:
            cx, cy = float(pos[0]), float(pos[1])
        except Exception:
            pass

    area: Optional[float] = None
    try:
        if Circle is not None and isinstance(prim, Circle):
            area = pi * float(prim.radius) ** 2
        elif Rectangle is not None and isinstance(prim, Rectangle):
            area = float(prim.axis_aligned_width) * float(prim.axis_aligned_height)
        elif Obround is not None and isinstance(prim, Obround):
            w = float(prim.axis_aligned_width)
            h = float(prim.axis_aligned_height)
            # stadium = bounding rectangle minus the four corner cutouts of the
            # semicircular end caps: w*h - (4 - pi) * r^2, r = half short side.
            r = 0.5 * min(w, h)
            area = w * h - (4 - pi) * r * r
        elif Ellipse is not None and isinstance(prim, Ellipse):
            area = pi * 0.5 * float(prim.width) * 0.5 * float(prim.height)
        elif Polygon is not None and isinstance(prim, Polygon):
            r = float(prim.radius)
            sides = int(getattr(prim, "sides", 6) or 6)
            area = 0.5 * sides * r * r * sin(2 * pi / max(3, sides))
    except Exception:
        area = None

    if area is None or area <= 0.0:
        area = max(0.0, (float(max_x) - float(min_x)) * (float(max_y) - float(min_y)))
    if area <= 0.0:
        return None
    return (area, cx, cy)


def _collect_pads(path) -> List[Tuple[float, float, float]]:
    """Parse a Gerber file and return flashed pads as (area_mm2, x_mm, y_mm)."""
    try:
        layer = gerber.read(str(path))
    except Exception:
        return []
    try:
        layer.to_metric()
    except Exception:
        pass
    pads: List[Tuple[float, float, float]] = []
    for prim in getattr(layer, "primitives", []) or []:
        info = _pad_area_and_pos(prim)
        if info is not None:
            pads.append(info)
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

    if gerber is None:
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
    worst: Optional[Tuple[float, float, float]] = None  # (coverage, x, y)

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
            # Track the pad furthest from the ideal mid-range coverage.
            mid = 0.5 * (target_min + target_max)
            if worst is None or abs(cov - mid) > abs(worst[0] - mid):
                worst = (cov, px, py)

    if not coverages:
        return _na(ctx, target_min, limit_min, limit_max,
                   "Paste layer present but no paste apertures could be matched to copper pads; not applicable.")

    measured = sum(coverages) / len(coverages)

    # Two-sided range grading.
    if measured < limit_min or measured > limit_max:
        status = "fail"
    elif measured < target_min or measured > target_max:
        status = "warning"
    else:
        status = "pass"

    if target_min <= measured <= target_max:
        score = 100.0
    elif measured < target_min:
        span = max(1e-9, target_min - limit_min)
        score = max(0.0, min(100.0, 100.0 * (measured - limit_min) / span))
    else:  # measured > target_max
        span = max(1e-9, limit_max - target_max)
        score = max(0.0, min(100.0, 100.0 * (limit_max - measured) / span))

    # margin to the nearer limit (positive = inside limits)
    margin = min(measured - limit_min, limit_max - measured)

    violations: List[Violation] = []
    if status != "pass":
        loc = None
        if worst is not None:
            loc = ViolationLocation(
                layer="Paste", x_mm=worst[1], y_mm=worst[2],
                notes="Pad with paste coverage furthest from the recommended range.",
            )
        violations.append(Violation(
            severity=_severity(ctx),
            message=(
                f"Mean solder-paste coverage {measured:.1f}% across {len(coverages)} pad(s) "
                f"is outside the recommended {target_min:.0f}-{target_max:.0f}% "
                f"(allowed {limit_min:.0f}-{limit_max:.0f}%)."
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
