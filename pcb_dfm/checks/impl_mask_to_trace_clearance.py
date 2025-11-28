from __future__ import annotations

from typing import List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, Violation, ViolationLocation


@register_check("mask_to_trace_clearance")
def run_mask_to_trace_clearance(ctx: CheckContext) -> CheckResult:
    """
    Approximate solder mask to copper clearance (mask expansion).

    For each mask polygon on a given side:
      - Consider only "pad-like" copper polygons whose bbox is fully contained
        in the mask bbox and which satisfy:
          * c_area >= pad_min_area_mm2
          * c_short_dim >= pad_min_short_dim_mm
          * c_aspect_ratio <= pad_max_aspect_ratio
      - For each mask + pad pair:
          * m_width, m_height = bbox(mask)
          * c_width, c_height = bbox(copper)
          * ex = (m_width  - c_width)  / 2
          * ey = (m_height - c_height) / 2
            expansion = max(min(ex, ey), 0.0)
      - Report the minimum expansion across all such pairs.

    Special case:
      - If min_expansion <= epsilon_zero, treat as "warning" rather than hard fail,
        since many designs use zero mask expansion and bbox heuristics can quantize.
    """
    metric_cfg = ctx.check_def.metric or {}
    units_raw = metric_cfg.get("units", metric_cfg.get("unit", "mm"))
    units = "mm" if units_raw in (None, "", "mm", "um") else units_raw

    limits = ctx.check_def.limits or {}
    recommended_min = float(limits.get("recommended_min", 0.05))  # mm
    absolute_min = float(limits.get("absolute_min", 0.03))        # mm

    raw_cfg = ctx.check_def.raw or {}

    # Pad classification thresholds (mm / mm^2)
    pad_min_area_mm2 = float(raw_cfg.get("pad_min_area_mm2", 0.02))
    pad_min_short_dim_mm = float(raw_cfg.get("pad_min_short_dim_mm", 0.2))
    pad_max_aspect_ratio = float(raw_cfg.get("pad_max_aspect_ratio", 3.0))
    mask_min_area_mm2 = float(raw_cfg.get("mask_min_area_mm2", 0.02))

    # Special-case threshold for "effectively zero" expansion
    epsilon_zero = float(raw_cfg.get("epsilon_zero", 0.005))

    geom = ctx.geometry

    # Partition layers by type and side
    mask_layers = []
    copper_layers_by_side: dict[str, List] = {}

    for layer in geom.layers:
        layer_type = getattr(layer, "layer_type", getattr(layer, "type", None))
        side = getattr(layer, "side", "Unknown") or "Unknown"

        if layer_type == "mask":
            mask_layers.append(layer)
        elif layer_type == "copper":
            copper_layers_by_side.setdefault(side, []).append(layer)

    if not mask_layers or not copper_layers_by_side:
        viol = Violation(
            severity="warning",
            message="No mask or copper layers available to compute mask to trace clearance.",
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

    min_expansion: Optional[float] = None
    worst_location: Optional[ViolationLocation] = None

    for mask_layer in mask_layers:
        side = getattr(mask_layer, "side", "Unknown") or "Unknown"
        copper_layers = copper_layers_by_side.get(side, [])
        if not copper_layers:
            continue

        # Precollect copper bboxes for speed
        copper_polys: List[Tuple[float, float, float, float, str]] = []
        for c_layer in copper_layers:
            for poly in c_layer.polygons:
                b = poly.bounds()
                copper_polys.append(
                    (b.min_x, b.max_x, b.min_y, b.max_y, c_layer.logical_layer)
                )

        for m_poly in mask_layer.polygons:
            mb = m_poly.bounds()
            m_width = max(mb.max_x - mb.min_x, 0.0)
            m_height = max(mb.max_y - mb.min_y, 0.0)
            if m_width <= 0.0 or m_height <= 0.0:
                continue

            mask_area = m_width * m_height
            if mask_area < mask_min_area_mm2:
                # extremely small mask opening, ignore
                continue

            best_local_expansion: Optional[float] = None
            best_local_copper_layer: Optional[str] = None

            for c_min_x, c_max_x, c_min_y, c_max_y, c_layer_name in copper_polys:
                # require copper bbox fully inside mask bbox
                if (
                    c_min_x < mb.min_x
                    or c_max_x > mb.max_x
                    or c_min_y < mb.min_y
                    or c_max_y > mb.max_y
                ):
                    continue

                c_width = max(c_max_x - c_min_x, 0.0)
                c_height = max(c_max_y - c_min_y, 0.0)
                if c_width <= 0.0 or c_height <= 0.0:
                    continue

                c_area = c_width * c_height
                c_short_dim = min(c_width, c_height)
                c_long_dim = max(c_width, c_height)
                if c_short_dim <= 0.0:
                    continue

                c_aspect_ratio = c_long_dim / c_short_dim

                # pad classification filters
                if c_area < pad_min_area_mm2:
                    continue
                if c_short_dim < pad_min_short_dim_mm:
                    continue
                if c_aspect_ratio > pad_max_aspect_ratio:
                    continue

                ex = 0.5 * (m_width - c_width)
                ey = 0.5 * (m_height - c_height)
                expansion = min(ex, ey)
                if expansion < 0.0:
                    # mask smaller than copper bbox (negative expansion)
                    expansion = 0.0

                if best_local_expansion is None or expansion < best_local_expansion:
                    best_local_expansion = expansion
                    best_local_copper_layer = c_layer_name

            if best_local_expansion is None:
                continue

            if min_expansion is None or best_local_expansion < min_expansion:
                min_expansion = best_local_expansion
                cx = 0.5 * (mb.min_x + mb.max_x)
                cy = 0.5 * (mb.min_y + mb.max_y)
                worst_location = ViolationLocation(
                    layer=mask_layer.logical_layer,
                    x_mm=cx,
                    y_mm=cy,
                    notes=f"Mask opening over copper layer {best_local_copper_layer}.",
                )

    if min_expansion is None:
        viol = Violation(
            severity="warning",
            message="No mask openings enclosing pad-like copper; cannot estimate mask expansion.",
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

    # Decide status with special handling for "effectively zero" expansion
    if min_expansion <= epsilon_zero:
        status = "warning"
        severity = "warning"
        score = 50.0
    elif min_expansion < absolute_min:
        status = "fail"
        severity = "error"
        # score computed below
        score = 0.0  # will be overwritten by interpolation
    elif min_expansion < recommended_min:
        status = "warning"
        severity = "warning"
        score = 75.0  # mid warning - you can tune
    else:
        status = "pass"
        severity = ctx.check_def.severity or "error"
        score = 100.0

    if status == "fail":
        if min_expansion <= absolute_min:
            score = 0.0
        else:
            span = recommended_min - absolute_min
            score = max(0.0, min(100.0, 100.0 * (min_expansion - absolute_min) / span))

    margin_to_limit = float(min_expansion - absolute_min)

    violations: List[Violation] = []
    if status != "pass":
        if min_expansion <= epsilon_zero:
            msg = (
                f"Mask openings appear to have zero expansion relative to copper (measured {min_expansion:.3f} mm). "
                f"Many fabs prefer positive mask expansion; confirm with your fab or update design rules."
            )
        else:
            msg = (
                f"Minimum mask to trace clearance (mask expansion) {min_expansion:.3f} mm "
                f"is below recommended {recommended_min:.3f} mm (absolute minimum {absolute_min:.3f} mm)."
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
            "measured_value": float(min_expansion),
            "target": recommended_min,
            "limit_low": absolute_min,
            "limit_high": None,
            "margin_to_limit": margin_to_limit,
        },
        violations=violations,
    )
