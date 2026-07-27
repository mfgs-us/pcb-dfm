"""HDI microvia geometry -- aspect ratio and single-dielectric span.

A laser-drilled microvia is plated by chemical/electro deposition down a blind
hole, so it only fills reliably when it is shallow relative to its diameter. The
governing rule is the **aspect ratio** = dielectric depth / drill diameter;
above ~0.75:1 plating thins at the bottom and above 1:1 it voids. A microvia
must also span exactly **one dielectric** (adjacent copper layers) -- a "microvia"
reaching across two dielectrics is not laser-drillable as drawn.

Depth comes from the stackup (the dielectric thickness between the two copper
layers the via names), so this is a definitive calc, not a heuristic. Without
microvias, or without the stackup/​drill data to size them, the check is
not_applicable rather than fabricated.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..ingest.design_model import Stackup, Via
from ..results import CheckResult, MetricResult, Violation, ViolationLocation


def _params(ctx: CheckContext) -> Tuple[float, float]:
    p = (ctx.check_def.raw or {}).get("params", {}) or {}
    return float(p.get("target_aspect", 0.75)), float(p.get("max_aspect", 1.0))


def _na(ctx: CheckContext, target: float, limit: float, msg: str) -> CheckResult:
    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status="not_applicable",
        severity="info",
        score=None,
        metric=MetricResult.dimensionless(None, target=target, limit_high=limit),
        violations=[Violation(severity="info", message=msg, location=None)],
    ).finalize()


def _span_depth(stackup: Stackup, a: Optional[str], b: Optional[str]
                ) -> Optional[Tuple[float, int]]:
    """(dielectric depth mm, #copper layers strictly between) for a via a<->b.

    Returns None when either layer name is absent from the stackup. The depth is
    the summed thickness of the layers between the two copper layers; a proper
    microvia has exactly one dielectric and zero copper between.
    """
    names = [ly.name for ly in stackup.layers]
    if a not in names or b not in names:
        return None
    i, j = sorted((names.index(a), names.index(b)))
    between = stackup.layers[i + 1:j]
    depth = sum(ly.thickness_mm for ly in between if ly.thickness_mm)
    copper_between = sum(1 for ly in between if ly.kind == "copper")
    return depth, copper_between


@register_check("microvia_geometry")
def run_microvia_geometry(ctx: CheckContext) -> CheckResult:
    target_aspect, max_aspect = _params(ctx)

    dd = ctx.design_data
    if dd is None:
        return _na(ctx, target_aspect, max_aspect,
                   "No design data; microvia geometry not applicable.")
    micros: List[Via] = [
        v for net in dd.nets.values() for v in net.vias if v.via_type == "micro"]
    if not micros:
        return _na(ctx, target_aspect, max_aspect,
                   "No microvias in the design; microvia geometry not applicable.")
    if dd.stackup is None or not dd.stackup.layers:
        return _na(ctx, target_aspect, max_aspect,
                   f"{len(micros)} microvia(s) present but no stackup to size their depth; not applicable.")

    worst_aspect: Optional[float] = None
    worst_loc: Optional[Tuple[float, float]] = None
    fails: List[Tuple[float, float, float]] = []   # (x, y, aspect) aspect > max
    warns: List[Tuple[float, float, float]] = []   # target < aspect <= max
    multi_span: List[Tuple[float, float]] = []      # spans > 1 dielectric
    unsized = 0

    for v in micros:
        span = _span_depth(dd.stackup, v.from_layer, v.to_layer)
        if span is None:
            unsized += 1
            continue
        depth, copper_between = span
        if copper_between > 0:
            multi_span.append((v.x_mm, v.y_mm))
        if not v.drill_mm or v.drill_mm <= 0.0 or depth <= 0.0:
            unsized += 1
            continue
        aspect = depth / v.drill_mm
        if worst_aspect is None or aspect > worst_aspect:
            worst_aspect = aspect
            worst_loc = (v.x_mm, v.y_mm)
        if aspect > max_aspect:
            fails.append((v.x_mm, v.y_mm, aspect))
        elif aspect > target_aspect:
            warns.append((v.x_mm, v.y_mm, aspect))

    if worst_aspect is None and not multi_span:
        return _na(ctx, target_aspect, max_aspect,
                   f"{len(micros)} microvia(s) present but none could be sized from the stackup/​drill; not applicable.")

    if fails or multi_span:
        status = "fail"
    elif warns:
        status = "warning"
    else:
        status = "pass"

    violations: List[Violation] = []
    for (x, y, aspect) in fails:
        violations.append(Violation(
            severity="error",
            message=f"Microvia at ({x:.2f}, {y:.2f}) aspect ratio {aspect:.2f}:1 exceeds the {max_aspect:.2f}:1 limit -> plating voids.",
            location=ViolationLocation(layer="Via", x_mm=x, y_mm=y,
                                       notes="Microvia too deep for its diameter."),
        ))
    for (x, y) in multi_span:
        violations.append(Violation(
            severity="error",
            message=f"Microvia at ({x:.2f}, {y:.2f}) spans more than one dielectric -> not laser-drillable as a single microvia (use stacked/​staggered microvias or a buried via).",
            location=ViolationLocation(layer="Via", x_mm=x, y_mm=y,
                                       notes="Microvia spans multiple dielectrics."),
        ))
    for (x, y, aspect) in warns:
        violations.append(Violation(
            severity="warning",
            message=f"Microvia at ({x:.2f}, {y:.2f}) aspect ratio {aspect:.2f}:1 exceeds the recommended {target_aspect:.2f}:1 (still under the {max_aspect:.2f}:1 limit).",
            location=ViolationLocation(layer="Via", x_mm=x, y_mm=y,
                                       notes="Microvia aspect ratio marginal."),
        ))
    if unsized:
        violations.append(Violation(
            severity="info",
            message=f"{unsized} microvia(s) could not be sized (layer not in stackup or missing drill) and were not graded.",
            location=None,
        ))
    if status == "pass" and not violations:
        violations.append(Violation(
            severity="info",
            message=f"{len(micros)} microvia(s), worst aspect ratio {worst_aspect:.2f}:1 -- within limits.",
            location=None,
        ))

    span = max(1e-9, max_aspect - target_aspect)
    if worst_aspect is None:
        score = 0.0  # multi-span only
    elif worst_aspect <= target_aspect:
        score = 100.0
    else:
        score = max(0.0, min(100.0, 100.0 * (max_aspect - worst_aspect) / span))
    if fails or multi_span:
        score = 0.0

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",
        score=score,
        metric=MetricResult.dimensionless(
            None if worst_aspect is None else float(worst_aspect),
            target=target_aspect, limit_high=max_aspect),
        violations=violations,
    ).finalize()
