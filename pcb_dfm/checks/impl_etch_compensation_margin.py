from __future__ import annotations

from typing import List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..ingest import GerberFileInfo
from ..results import CheckResult, MetricResult, Violation, ViolationLocation
from .impl_min_trace_width import _INCH_TO_MM, _get_line_width_inch

try:
    import gerber
    from gerber.primitives import Line
except Exception:  # pragma: no cover - defensive
    gerber = None
    Line = None  # type: ignore


def _thresholds(ctx: CheckContext) -> Tuple[float, float]:
    """Percent margin thresholds: target (recommended) and limit (absolute).

    A margin at or above ``target`` is comfortable; between ``limit`` and
    ``target`` is a yield watch item; below ``limit`` (default 0 %) means a
    feature sits at or under the process floor.
    """
    metric_cfg = ctx.check_def.metric or {}
    target_cfg = metric_cfg.get("target", {}) or {}
    limits_cfg = metric_cfg.get("limits", {}) or {}
    target_pct = float(target_cfg.get("min", 20.0))
    limit_pct = float(limits_cfg.get("min", 0.0))
    return target_pct, limit_pct


def _etch_floor_mm(ctx: CheckContext) -> float:
    """The minimum copper feature the etch process can reliably hold (mm).

    Ruleset/check overridable via ``raw.etch_capability_mm``; defaults to a
    conservative 0.075 mm line/space capability typical of standard 1 oz outer
    copper at a mainstream fab."""
    raw_cfg = getattr(ctx.check_def, "raw", None) or {}
    return float(raw_cfg.get("etch_capability_mm", 0.075))


def _na(ctx: CheckContext, target_pct: float, limit_pct: float, msg: str) -> CheckResult:
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="not_applicable",
        severity="info",
        score=None,
        metric=MetricResult.ratio_min_percent(None, target_pct=target_pct, limit_low_pct=limit_pct),
        violations=[Violation(severity="info", message=msg, location=None)],
    ).finalize()


@register_check("etch_compensation_margin")
def run_etch_compensation_margin(ctx: CheckContext) -> CheckResult:
    """Yield margin between the smallest copper feature and the etch floor.

    Over- and under-etch variation kills the narrowest copper first, so the
    features nearest the process's line/space capability carry the yield risk.
    We measure the worst-case margin as a percentage above the etch floor:

        margin% = (min_feature_width - etch_floor) / etch_floor * 100

    A feature at 0.09 mm over a 0.075 mm floor scores +20 %; a feature at or
    below the floor scores <= 0 %. Copper trace widths are read from Gerber
    ``Line`` primitives (the same honest source as ``min_trace_width``), not
    polygon bounding boxes.
    """
    target_pct, limit_pct = _thresholds(ctx)
    etch_floor = _etch_floor_mm(ctx)

    copper_files: List[GerberFileInfo] = [
        f for f in ctx.ingest.files if f.layer_type == "copper"
    ]
    if gerber is None or Line is None or not copper_files or etch_floor <= 0.0:
        return _na(ctx, target_pct, limit_pct,
                   "Cannot compute etch margin (missing Gerber parser, no copper, or invalid etch floor).")

    min_width_mm: Optional[float] = None
    worst_location: Optional[ViolationLocation] = None

    for info in copper_files:
        try:
            g_layer = gerber.read(str(info.path))
        except Exception:
            continue
        try:
            g_layer.to_inch()
        except Exception:
            pass

        for prim in getattr(g_layer, "primitives", []):
            if not isinstance(prim, Line):
                continue
            width_in = _get_line_width_inch(prim)
            if width_in is None:
                continue
            width_mm = width_in * _INCH_TO_MM
            if width_mm <= 0.0:
                continue
            if min_width_mm is None or width_mm < min_width_mm:
                min_width_mm = width_mm
                try:
                    x1_in, y1_in = prim.start
                    x2_in, y2_in = prim.end
                    worst_location = ViolationLocation(
                        layer=info.logical_layer,
                        x_mm=(x1_in + x2_in) * 0.5 * _INCH_TO_MM,
                        y_mm=(y1_in + y2_in) * 0.5 * _INCH_TO_MM,
                        notes="Narrowest copper feature relative to the etch floor.",
                    )
                except Exception:
                    worst_location = None

    if min_width_mm is None:
        return _na(ctx, target_pct, limit_pct,
                   "No copper trace segments found to estimate etch margin.")

    margin_pct = (min_width_mm - etch_floor) / etch_floor * 100.0

    if margin_pct < limit_pct:
        status = "fail"
    elif margin_pct < target_pct:
        status = "warning"
    else:
        status = "pass"

    # Linear score across the [limit, target] band.
    if margin_pct >= target_pct:
        score = 100.0
    elif margin_pct <= limit_pct:
        score = 0.0
    else:
        span = max(1e-9, target_pct - limit_pct)
        score = max(0.0, min(100.0, 100.0 * (margin_pct - limit_pct) / span))

    violations: List[Violation] = []
    if status != "pass":
        violations.append(Violation(
            severity=(ctx.check_def.severity or "info") if status == "warning" else "warning",
            message=(
                f"Narrowest copper feature is {min_width_mm:.3f} mm, only {margin_pct:.1f}% above the "
                f"{etch_floor:.3f} mm etch floor (recommended margin {target_pct:.0f}%)."
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
        metric=MetricResult.ratio_min_percent(
            measured_pct=float(margin_pct),
            target_pct=target_pct,
            limit_low_pct=limit_pct,
        ),
        violations=violations,
    ).finalize()
