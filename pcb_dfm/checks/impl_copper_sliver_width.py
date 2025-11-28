from __future__ import annotations

from typing import List, Optional

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, Violation, ViolationLocation
from ..geometry import queries


@register_check("copper_sliver_width")
def run_copper_sliver_width(ctx: CheckContext) -> CheckResult:
    """
    Detect narrow copper "slivers" based on polygon bounding boxes.

    Approximation:
      - For each copper polygon:
          * compute bbox (width, height) in mm
          * area = width * height
          * aspect_ratio = long_dim / short_dim
      - Consider as sliver candidates only if:
          * area >= min_area_mm2
          * aspect_ratio >= min_aspect_ratio
          * long_dim >= min_long_dim_mm
          * min_candidate_short_dim_mm <= short_dim <= max_short_dim_mm
          * area >= ignore_tiny_feature_area_mm2
      - Sliver width = short_dim
      - Report minimum sliver width across all candidates.
    """
    metric_cfg = ctx.check_def.metric or {}
    units_raw = metric_cfg.get("units", metric_cfg.get("unit", "mm"))
    units = "mm" if units_raw in (None, "", "mm", "um") else units_raw

    limits = ctx.check_def.limits or {}
    recommended_min = float(limits.get("recommended_min", 0.15))  # mm
    absolute_min = float(limits.get("absolute_min", 0.10))        # mm

    raw_cfg = ctx.check_def.raw or {}

    # Filtering thresholds (all in mm / mm^2)
    min_area_mm2 = float(raw_cfg.get("min_area_mm2", 0.02))
    min_aspect_ratio = float(raw_cfg.get("min_aspect_ratio", 4.0))
    min_long_dim_mm = float(raw_cfg.get("min_long_dim_mm", 0.5))
    max_short_dim_mm = float(raw_cfg.get("max_short_dim_mm", 0.3))
    ignore_tiny_feature_area_mm2 = float(raw_cfg.get("ignore_tiny_feature_area_mm2", 0.005))
    # New: lower bound on candidate short dimension so we ignore ultra tiny artifacts
    min_candidate_short_dim_mm = float(raw_cfg.get("min_candidate_short_dim_mm", 0.05))

    copper_layers = queries.get_copper_layers(ctx.geometry)
    if not copper_layers:
        viol = Violation(
            severity="warning",
            message="No copper layers available to compute copper sliver width.",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            severity=ctx.check_def.severity,
            status="warning",
            score=50.0,
            metric={
                "kind": "geometry",
                "units": units,
                "measured_value": None,
                "target": recommended_min,
                "limit_low": absolute_min,
                "limit_high": None,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    min_sliver: Optional[float] = None
    worst_location: Optional[ViolationLocation] = None

    for layer in copper_layers:
        for poly in layer.polygons:
            b = poly.bounds()
            width = max(b.max_x - b.min_x, 0.0)
            height = max(b.max_y - b.min_y, 0.0)
            if width <= 0.0 or height <= 0.0:
                continue

            area = width * height
            if area < ignore_tiny_feature_area_mm2:
                # extremely small artifact, ignore entirely
                continue

            short_dim = min(width, height)
            long_dim = max(width, height)
            if short_dim <= 0.0:
                continue

            aspect_ratio = long_dim / short_dim

            # sliver candidate filters
            if area < min_area_mm2:
                continue
            if aspect_ratio < min_aspect_ratio:
                continue
            if long_dim < min_long_dim_mm:
                continue
            # New window on short dimension: ignore ultra tiny and clearly non sliver wide features
            if short_dim < min_candidate_short_dim_mm:
                continue
            if short_dim > max_short_dim_mm:
                continue

            sliver_width = short_dim
            if min_sliver is None or sliver_width < min_sliver:
                min_sliver = sliver_width
                cx = 0.5 * (b.min_x + b.max_x)
                cy = 0.5 * (b.min_y + b.max_y)
                worst_location = ViolationLocation(
                    layer=layer.logical_layer,
                    x_mm=cx,
                    y_mm=cy,
                    notes="Narrow copper region (sliver) based on polygon bounding box.",
                )

    if min_sliver is None:
        viol = Violation(
            severity="info",
            message="No elongated copper regions found that match sliver criteria.",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            severity=ctx.check_def.severity,
            status="pass",
            score=100.0,
            metric={
                "kind": "geometry",
                "units": units,
                "measured_value": None,
                "target": recommended_min,
                "limit_low": absolute_min,
                "limit_high": None,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    # status
    if min_sliver < absolute_min:
        status = "fail"
        severity = "error"
    elif min_sliver < recommended_min:
        status = "warning"
        severity = "warning"
    else:
        status = "pass"
        severity = ctx.check_def.severity or "error"

    # score
    if min_sliver >= recommended_min:
        score = 100.0
    elif min_sliver <= absolute_min:
        score = 0.0
    else:
        span = recommended_min - absolute_min
        score = max(0.0, min(100.0, 100.0 * (min_sliver - absolute_min) / span))

    margin_to_limit = float(min_sliver - absolute_min)

    violations: List[Violation] = []
    if status != "pass":
        msg = (
            f"Minimum copper sliver width {min_sliver:.3f} mm is below "
            f"recommended {recommended_min:.3f} mm (absolute minimum {absolute_min:.3f} mm)."
        )
        violations.append(
            Violation(
                severity=severity,
                message=msg,
                location=worst_location,
            )
        )

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        severity=ctx.check_def.severity,
        status=status,
        score=score,
        metric={
            "kind": "geometry",
            "units": units,
            "measured_value": float(min_sliver),
            "target": recommended_min,
            "limit_low": absolute_min,
            "limit_high": None,
            "margin_to_limit": margin_to_limit,
        },
        violations=violations,
    )
