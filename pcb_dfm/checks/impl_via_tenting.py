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


@register_check("via_tenting")
def run_via_tenting(ctx: CheckContext) -> CheckResult:
    """
    Approximate via tenting by treating small roundish copper pads as vias and
    checking for mask openings over their centers.

    Metric is percentage of vias that are tented on each side (combined).
    """
    metric_cfg = ctx.check_def.metric or {}

    target_raw = metric_cfg.get("target")
    if isinstance(target_raw, dict):
        target_cfg = target_raw
    else:
        target_cfg = {}

    limits_raw = metric_cfg.get("limits")
    if isinstance(limits_raw, dict):
        limits_cfg = limits_raw
    else:
        limits_cfg = {}

    recommended_min = float(target_cfg.get("min", 80.0))  # percent tented
    absolute_min = float(limits_cfg.get("min", 50.0))

    raw_cfg = getattr(ctx.check_def, "raw", None) or {}
    via_pad_min_area_mm2 = float(raw_cfg.get("via_pad_min_area_mm2", 0.01))
    via_pad_max_area_mm2 = float(raw_cfg.get("via_pad_max_area_mm2", 0.5))
    via_pad_max_aspect_ratio = float(raw_cfg.get("via_pad_max_aspect_ratio", 1.5))
    mask_min_area_mm2 = float(raw_cfg.get("mask_min_area_mm2", 0.02))
    mask_center_tolerance_mm = float(raw_cfg.get("mask_center_tolerance_mm", 0.02))

    geom = ctx.geometry

    class _Via:
        __slots__ = ("side", "layer", "cx", "cy")

        def __init__(self, side, layer, cx, cy):
            self.side = side
            self.layer = layer
            self.cx = cx
            self.cy = cy

    class _MaskOpening:
        __slots__ = ("side", "layer", "min_x", "max_x", "min_y", "max_y")

        def __init__(self, side, layer, poly):
            self.side = side
            self.layer = layer
            b = poly.bounds()
            self.min_x = b.min_x
            self.max_x = b.max_x
            self.min_y = b.min_y
            self.max_y = b.max_y

        def contains(self, x: float, y: float, tol: float) -> bool:
            return (
                self.min_x - tol <= x <= self.max_x + tol
                and self.min_y - tol <= y <= self.max_y + tol
            )

    vias: List[_Via] = []
    masks: List[_MaskOpening] = []

    for layer in getattr(geom, "layers", []):
        layer_type = getattr(layer, "layer_type", getattr(layer, "type", None))
        side = getattr(layer, "side", None)
        logical = getattr(layer, "logical_layer", getattr(layer, "name", None))

        if layer_type == "copper":
            for poly in getattr(layer, "polygons", []):
                area = _poly_area_mm2(poly)
                if area < via_pad_min_area_mm2 or area > via_pad_max_area_mm2:
                    continue
                b = poly.bounds()
                w = max(0.0, b.max_x - b.min_x)
                h = max(0.0, b.max_y - b.min_y)
                if w <= 0.0 or h <= 0.0:
                    continue
                short_dim = min(w, h)
                long_dim = max(w, h)
                aspect = long_dim / short_dim if short_dim > 0.0 else 1.0
                if aspect > via_pad_max_aspect_ratio:
                    continue

                cx = 0.5 * (b.min_x + b.max_x)
                cy = 0.5 * (b.min_y + b.max_y)
                vias.append(_Via(side, logical, cx, cy))

        elif layer_type == "mask":
            for poly in getattr(layer, "polygons", []):
                area = _poly_area_mm2(poly)
                if area < mask_min_area_mm2:
                    continue
                masks.append(_MaskOpening(side, logical, poly))

    if not vias:
        viol = Violation(
            severity="info",
            message="No via like pads detected to evaluate tenting.",
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
                "kind": "ratio",
                "units": "%",
                "measured_value": None,
                "target": recommended_min,
                "limit_low": absolute_min,
                "limit_high": None,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    tented_count = 0
    exposed_count = 0
    first_exposed_loc: Optional[ViolationLocation] = None

    for v in vias:
        has_opening = False
        for m in masks:
            if v.side and m.side and str(v.side).lower() != str(m.side).lower():
                continue
            if m.contains(v.cx, v.cy, mask_center_tolerance_mm):
                has_opening = True
                break
        if has_opening:
            exposed_count += 1
            if first_exposed_loc is None:
                first_exposed_loc = ViolationLocation(
                    layer=v.layer,
                    x_mm=v.cx,
                    y_mm=v.cy,
                    notes="Via like pad with mask opening over center (untented).",
                )
        else:
            tented_count += 1

    total = tented_count + exposed_count
    if total <= 0:
        viol = Violation(
            severity="info",
            message="No vias evaluated for tenting due to filtering.",
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
                "kind": "ratio",
                "units": "%",
                "measured_value": None,
                "target": recommended_min,
                "limit_low": absolute_min,
                "limit_high": None,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    tented_pct = 100.0 * tented_count / float(total)

    # 5C) Default to Warning/Info instead of Fail to match Integr8tor
    # Integr8tor doesn't enforce tenting ratios
    
    # Check if user has opted into strict assembly risk profile
    raw_cfg = getattr(ctx.check_def, "raw", None) or {}
    strict_assembly_mode = raw_cfg.get("strict_assembly_mode", False)

    if tented_pct >= recommended_min:
        status = "pass"
        severity = ctx.check_def.severity or "info"  # Default to info
        score = 100.0
    elif tented_pct < absolute_min:
        # Only fail if user has opted into strict assembly mode
        if strict_assembly_mode:
            status = "fail"
            severity = "error"
            score = 0.0
        else:
            status = "warning"  # Default to warning instead of fail
            severity = "warning"
            score = 40.0  # Lower score for warning but not failure
    else:
        status = "warning"
        severity = "warning"
        span = max(1e-6, recommended_min - absolute_min)
        frac = (tented_pct - absolute_min) / span
        score = max(0.0, min(100.0, 60.0 + 40.0 * max(0.0, frac)))

    margin_to_limit = float(tented_pct - absolute_min)

    msg = (
        f"Estimated via tenting: {tented_pct:.1f}% tented "
        f"({tented_count} tented / {exposed_count} exposed, total {total}). "
        f"Recommended >= {recommended_min:.1f}% tented, absolute >= {absolute_min:.1f}%."
    )

    violations: List[Violation] = []
    if status != "pass":
        violations.append(
            Violation(
                severity=severity,
                message=msg,
                location=first_exposed_loc,
            )
        )
    else:
        violations.append(
            Violation(
                severity="info",
                message=msg,
                location=None,
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
            "kind": "ratio",
            "units": "%",
            "measured_value": tented_pct,
            "target": recommended_min,
            "limit_low": absolute_min,
            "limit_high": None,
            "margin_to_limit": margin_to_limit,
        },
        violations=violations,
    )
