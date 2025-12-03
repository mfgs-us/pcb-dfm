from __future__ import annotations

import math
from typing import List, Optional

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, Violation, ViolationLocation


def _poly_area_mm2(poly) -> float:
    if hasattr(poly, "area_mm2"):
        return float(poly.area_mm2)
    if hasattr(poly, "area"):
        try:
            return float(poly.area())
        except TypeError:
            return float(poly.area)
    b = poly.bounds()
    return max(0.0, (b.max_x - b.min_x) * (b.max_y - b.min_y))


@register_check("silkscreen_min_width")
def run_silkscreen_min_width(ctx: CheckContext) -> CheckResult:
    """
    Estimate minimum silkscreen feature width using bounding box short dimension
    of reasonably sized silkscreen polygons.

    Internal geometry in mm, metric reported in mm.
    """
    metric_cfg = ctx.check_def.metric or {}
    target_raw = metric_cfg.get("target", {}) or {}
    limits_raw = metric_cfg.get("limits", {}) or {}

    units_raw = (metric_cfg.get("units") or "mm").lower()
    source_is_um = units_raw in ("um", "micron", "microns")

    if isinstance(target_raw, dict):
        raw_target_min = target_raw.get("min", 100.0 if source_is_um else 0.1)
    else:
        raw_target_min = 100.0 if source_is_um else 0.1

    if isinstance(limits_raw, dict):
        raw_abs_min = limits_raw.get("min", 80.0 if source_is_um else 0.08)
    else:
        raw_abs_min = 80.0 if source_is_um else 0.08

    scale = 0.001 if source_is_um else 1.0
    recommended_min = float(raw_target_min) * scale
    absolute_min = float(raw_abs_min) * scale

    raw_cfg = getattr(ctx.check_def, "raw", None) or {}
    min_feature_area_mm2 = float(raw_cfg.get("min_feature_area_mm2", 0.01))
    min_feature_length_mm = float(raw_cfg.get("min_feature_length_mm", 0.2))
    max_aspect_ratio = float(raw_cfg.get("max_aspect_ratio", 30.0))

    geom = ctx.geometry

    min_width = math.inf
    min_loc: Optional[ViolationLocation] = None

    for layer in getattr(geom, "layers", []):
        layer_type = getattr(layer, "layer_type", getattr(layer, "type", None))
        if layer_type not in ("silkscreen", "silk"):
            continue

        logical = getattr(layer, "logical_layer", getattr(layer, "name", None))

        for poly in getattr(layer, "polygons", []):
            area = _poly_area_mm2(poly)
            if area < min_feature_area_mm2:
                continue

            b = poly.bounds()
            w = max(0.0, b.max_x - b.min_x)
            h = max(0.0, b.max_y - b.min_y)
            if w <= 0.0 or h <= 0.0:
                continue

            short_dim = min(w, h)
            long_dim = max(w, h)

            if long_dim < min_feature_length_mm:
                continue

            aspect = long_dim / short_dim if short_dim > 0.0 else 1.0
            if aspect > max_aspect_ratio:
                continue

            if short_dim < min_width:
                min_width = short_dim
                cx = 0.5 * (b.min_x + b.max_x)
                cy = 0.5 * (b.min_y + b.max_y)
                min_loc = ViolationLocation(
                    layer=logical,
                    x_mm=cx,
                    y_mm=cy,
                    notes="Narrowest silkscreen feature (approximate).",
                )

    if not math.isfinite(min_width):
        viol = Violation(
            severity="info",
            message="No eligible silkscreen features found to estimate minimum width.",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            severity=ctx.check_def.severity or ctx.check_def.severity_default,
            status="pass",
            score=100.0,
            metric={
                "kind": "geometry",
                "units": "mm",
                "measured_value": None,
                "target": recommended_min,
                "limit_low": absolute_min,
                "limit_high": None,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    measured = float(min_width)

    if measured >= recommended_min:
        status = "pass"
        severity = ctx.check_def.severity or ctx.check_def.severity_default
        score = 100.0
    elif measured < absolute_min:
        status = "fail"
        severity = "error"
        score = 0.0
    else:
        status = "warning"
        severity = "warning"
        span = max(1e-6, recommended_min - absolute_min)
        frac = (measured - absolute_min) / span
        score = max(0.0, min(100.0, 60.0 + 40.0 * max(0.0, frac)))

    margin_to_limit = float(measured - absolute_min)

    msg = (
        f"Minimum silkscreen feature width is {measured:.3f} mm "
        f"(recommended >= {recommended_min:.3f} mm, absolute >= {absolute_min:.3f} mm)."
    )

    violations: List[Violation] = []
    if status != "pass":
        violations.append(
            Violation(
                severity=severity,
                message=msg,
                location=min_loc,
            )
        )

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        severity=ctx.check_def.severity or ctx.check_def.severity_default,
        status=status,
        score=score,
        metric={
            "kind": "geometry",
            "units": "mm",
            "measured_value": measured,
            "target": recommended_min,
            "limit_low": absolute_min,
            "limit_high": None,
            "margin_to_limit": margin_to_limit,
        },
        violations=violations,
    )
