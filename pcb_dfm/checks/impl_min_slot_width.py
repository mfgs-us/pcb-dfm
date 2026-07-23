from __future__ import annotations

from math import hypot
from typing import List, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.gerber_backend import GERBONARA_AVAILABLE, excellon_slots_mm
from ..results import CheckResult, MetricResult, Violation, ViolationLocation

_INCH_TO_MM = 25.4


def _thresholds(ctx: CheckContext) -> Tuple[float, float]:
    """Read target/limit minimum slot width (mm) from the metric definition.

    These JSON checks store the numbers under metric.target/metric.limits
    (not the top-level check_def.limits), matching the pattern used by
    component_to_component_spacing.
    """
    metric_cfg = ctx.check_def.metric or {}
    target_cfg = metric_cfg.get("target", {}) or {}
    limits_cfg = metric_cfg.get("limits", {}) or {}
    target_min = float(target_cfg.get("min", 0.8))
    limit_min = float(limits_cfg.get("min", 0.6))
    return target_min, limit_min


def _severity(ctx: CheckContext) -> str:
    return (
        ctx.check_def.raw.get("severity_default")
        or ctx.check_def.severity
        or "warning"
    )


def _collect_slots_mm(ctx: CheckContext) -> List[Tuple[float, float, float, float]]:
    """Return detected slots as (width_mm, length_mm, cx_mm, cy_mm).

    Slots are routed/elongated features: the routing tool diameter is the slot
    *width* (the narrow dimension a tool must fit) and the start->end distance is
    its length. Parsed via the gerbonara backend (#3), which reports both in mm.
    """
    slots: List[Tuple[float, float, float, float]] = []
    for f in ctx.ingest.files:
        if f.layer_type != "drill":
            continue
        for s in excellon_slots_mm(f.path):
            if s.width_mm <= 0.0:
                continue
            length = hypot(s.x2_mm - s.x1_mm, s.y2_mm - s.y1_mm)
            slots.append((
                s.width_mm, length,
                0.5 * (s.x1_mm + s.x2_mm), 0.5 * (s.y1_mm + s.y2_mm),
            ))
    return slots
@register_check("min_slot_width")
def run_min_slot_width(ctx: CheckContext) -> CheckResult:
    """Minimum routed/drilled slot width.

    Measures the narrowest slot feature (Excellon routed slots or slot
    primitives). If no slots are present in the design, the check is
    genuinely not applicable (no fabricated measurement).
    """
    target_min, limit_min = _thresholds(ctx)

    if not GERBONARA_AVAILABLE:
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="not_applicable",
            severity="info",
            score=None,
            metric=MetricResult.geometry_mm(None, target_mm=target_min, limit_low_mm=limit_min),
            violations=[Violation(
                severity="info",
                message="Excellon parser unavailable; cannot measure slot width.",
                location=None,
            )],
        ).finalize()

    slots = _collect_slots_mm(ctx)
    if not slots:
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="not_applicable",
            severity="info",
            score=None,
            metric=MetricResult.geometry_mm(None, target_mm=target_min, limit_low_mm=limit_min),
            violations=[Violation(
                severity="info",
                message="No Excellon drill/rout slots found; slot width not applicable (milled slots defined on Gerber layers are not measured).",
                location=None,
            )],
        ).finalize()

    # Narrowest slot drives the result.
    min_width, length, cx, cy = min(slots, key=lambda s: s[0])

    if min_width < limit_min:
        status = "fail"
    elif min_width < target_min:
        status = "warning"
    else:
        status = "pass"

    if min_width >= target_min:
        score = 100.0
    elif min_width <= limit_min:
        score = 0.0
    else:
        span = max(1e-9, target_min - limit_min)
        score = max(0.0, min(100.0, 100.0 * (min_width - limit_min) / span))

    violations: List[Violation] = []
    if status != "pass":
        violations.append(Violation(
            severity=_severity(ctx),
            message=(
                f"Narrowest slot width {min_width:.3f} mm (length {length:.2f} mm) is below "
                f"recommended {target_min:.3f} mm (absolute minimum {limit_min:.3f} mm)."
            ),
            location=ViolationLocation(
                layer="Drill",
                x_mm=cx,
                y_mm=cy,
                notes="Narrowest routed/drilled slot feature.",
            ),
        ))

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        status=status,
        severity="info",
        score=score,
        metric=MetricResult.geometry_mm(
            measured_mm=float(min_width),
            target_mm=target_min,
            limit_low_mm=limit_min,
        ),
        violations=violations,
    ).finalize()
