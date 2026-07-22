"""
plating_uniformity — estimate variation in plated copper thickness across holes.

Electroplating "throwing power" falls as a hole gets deeper relative to its
diameter: a high aspect-ratio hole plates thinner at its centre than a shallow
one, and a board mixing very different hole sizes plates them to different
thicknesses at the same current density. So the *spread* of hole aspect ratios
is a first-order proxy for plating non-uniformity.

Model (explicitly heuristic — not a plating-bath simulation):

    t(AR) = 1 / (1 + c · max(0, AR − AR_knee))   # normalized centre-thickness
    non_uniformity% = 100 · (t_max − t_min) / t_max

Throwing power holds up to a knee (modern plating keeps >80% up to AR ~6-8) and
only degrades above it, so a routine mix of via and component holes — all below
the knee — reads as uniform (0%), while a genuinely high-aspect-ratio hole
alongside shallow ones diverges. ``AR_knee`` and ``c`` are raw-overridable.
Metric: non-uniformity % (minimize), target 10 / limit 20.
"""

from __future__ import annotations

from typing import List, Optional

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, MetricResult, Violation
from .impl_drill_aspect_ratio import _extract_tool_diameters_mm, _resolve_board_thickness_mm


def _thresholds(ctx: CheckContext) -> tuple[float, float]:
    m = ctx.check_def.metric or {}
    t = m.get("target") or {}
    limits = m.get("limits") or {}
    target = float(t.get("max", 10.0)) if isinstance(t, dict) else 10.0
    limit = float(limits.get("max", 20.0)) if isinstance(limits, dict) else 20.0
    return target, limit


def _plating_non_uniformity_pct(diameters_mm: List[float], thickness_mm: float,
                                c: float, ar_knee: float = 6.0) -> Optional[float]:
    """Normalized plating non-uniformity across a hole population (0..100).

    Aspect ratios below ``ar_knee`` are treated as fully plated (throwing power
    is effectively flat there); only holes above the knee reduce centre
    thickness and create spread."""
    ds = [d for d in diameters_mm if d > 0.0]
    if not ds or thickness_mm <= 0.0:
        return None
    ts = [1.0 / (1.0 + c * max(0.0, (thickness_mm / d) - ar_knee)) for d in ds]
    t_max, t_min = max(ts), min(ts)
    if t_max <= 0.0:
        return None
    return 100.0 * (t_max - t_min) / t_max


def _na(ctx: CheckContext, target: float, limit: float, msg: str) -> CheckResult:
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="not_applicable",
        severity="info",
        score=None,
        metric=MetricResult.ratio_percent(None, target_pct=target, limit_high_pct=limit),
        violations=[Violation(severity="info", message=msg, location=None)],
    ).finalize()


@register_check("plating_uniformity")
def run_plating_uniformity(ctx: CheckContext) -> CheckResult:
    target, limit = _thresholds(ctx)
    raw = ctx.check_def.raw or {}
    c = float(raw.get("throwing_power_falloff", 0.15))
    ar_knee = float(raw.get("aspect_ratio_knee", 6.0))

    drill_files = [f for f in ctx.ingest.files if f.layer_type == "drill"]
    if not drill_files:
        return _na(ctx, target, limit,
                   "No drill files; plating uniformity is estimated from the hole population.")

    diameters: List[float] = []
    for f in drill_files:
        diameters.extend(_extract_tool_diameters_mm(f.path))
    if not diameters:
        return _na(ctx, target, limit, "No drilled holes found to estimate plating uniformity.")

    thickness = _resolve_board_thickness_mm(ctx)
    measured = _plating_non_uniformity_pct(diameters, thickness, c, ar_knee)
    if measured is None:
        return _na(ctx, target, limit, "Insufficient hole geometry to estimate plating uniformity.")

    if measured > limit:
        status = "fail"
    elif measured > target:
        status = "warning"
    else:
        status = "pass"

    if measured <= target:
        score = 100.0
    elif measured >= limit:
        score = 0.0
    else:
        span = max(1e-9, limit - target)
        score = max(0.0, min(100.0, 100.0 * (limit - measured) / span))

    violations: List[Violation] = []
    if status != "pass":
        d_min, d_max = min(diameters), max(diameters)
        violations.append(Violation(
            severity=ctx.check_def.severity or "info",
            message=(
                f"Estimated plating non-uniformity {measured:.0f}% across holes "
                f"{d_min:.2f}–{d_max:.2f} mm on a {thickness:.2f} mm board "
                f"(target ≤ {target:.0f}%, limit ≤ {limit:.0f}%). Heuristic estimate."
            ),
            location=None,
        ))

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",
        score=score,
        metric=MetricResult.ratio_percent(
            measured_pct=float(measured), target_pct=target, limit_high_pct=limit,
        ),
        violations=violations,
    ).finalize()
