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


@register_check("solder_mask_expansion")
def run_solder_mask_expansion(ctx: CheckContext) -> CheckResult:
    """
    Estimate solder mask expansion around pad like copper features.

    For each pad candidate on copper layers, find the smallest enclosing mask
    opening on the same side, then compute an approximate expansion:
        expansion_mm = 0.5 * min(mask_w - pad_w, mask_h - pad_h)

    All internal geometry in mm.
    Metric is reported in mm, even if JSON used um.
    """
    metric_cfg = ctx.check_def.metric or {}
    target_raw = metric_cfg.get("target", {}) or {}
    limits_raw = metric_cfg.get("limits", {}) or {}

    # Normalize geometry thresholds to mm
    units_raw = (metric_cfg.get("units") or "mm").lower()
    source_is_um = units_raw in ("um", "micron", "microns")

    if isinstance(target_raw, dict):
        raw_target_min = target_raw.get("min", 50.0 if source_is_um else 0.05)
    else:
        raw_target_min = 50.0 if source_is_um else 0.05

    if isinstance(limits_raw, dict):
        raw_abs_min = limits_raw.get("min", 30.0 if source_is_um else 0.03)
    else:
        raw_abs_min = 30.0 if source_is_um else 0.03

    scale = 0.001 if source_is_um else 1.0  # um -> mm if needed
    recommended_min = float(raw_target_min) * scale
    absolute_min = float(raw_abs_min) * scale

    # Raw parameters
    raw_cfg = getattr(ctx.check_def, "raw", None) or {}
    pad_min_area_mm2 = float(raw_cfg.get("pad_min_area_mm2", 0.02))
    pad_max_area_mm2 = float(raw_cfg.get("pad_max_area_mm2", 4.0))
    pad_max_aspect_ratio = float(raw_cfg.get("pad_max_aspect_ratio", 10.0))
    mask_min_area_mm2 = float(raw_cfg.get("mask_min_area_mm2", 0.02))
    mask_search_inflate_mm = float(raw_cfg.get("mask_search_inflate_mm", 0.05))

    geom = ctx.geometry

    class _Pad:
        __slots__ = ("side", "layer", "poly", "min_x", "max_x", "min_y", "max_y", "cx", "cy")

        def __init__(self, side, layer, poly):
            self.side = side
            self.layer = layer
            self.poly = poly
            b = poly.bounds()
            self.min_x = b.min_x
            self.max_x = b.max_x
            self.min_y = b.min_y
            self.max_y = b.max_y
            self.cx = 0.5 * (b.min_x + b.max_x)
            self.cy = 0.5 * (b.min_y + b.max_y)

    class _MaskOpening:
        __slots__ = ("side", "layer", "poly", "min_x", "max_x", "min_y", "max_y", "area")

        def __init__(self, side, layer, poly):
            self.side = side
            self.layer = layer
            self.poly = poly
            b = poly.bounds()
            self.min_x = b.min_x
            self.max_x = b.max_x
            self.min_y = b.min_y
            self.max_y = b.max_y
            self.area = _poly_area_mm2(poly)

    pads: List[_Pad] = []
    masks: List[_MaskOpening] = []

    for layer in getattr(geom, "layers", []):
        layer_type = getattr(layer, "layer_type", getattr(layer, "type", None))
        side = getattr(layer, "side", None)
        logical = getattr(layer, "logical_layer", getattr(layer, "name", None))

        if layer_type == "copper":
            for poly in getattr(layer, "polygons", []):
                area = _poly_area_mm2(poly)
                if area < pad_min_area_mm2 or area > pad_max_area_mm2:
                    continue
                b = poly.bounds()
                w = max(0.0, b.max_x - b.min_x)
                h = max(0.0, b.max_y - b.min_y)
                if w <= 0.0 or h <= 0.0:
                    continue
                short_dim = min(w, h)
                long_dim = max(w, h)
                aspect = long_dim / short_dim if short_dim > 0.0 else 1.0
                if aspect > pad_max_aspect_ratio:
                    continue
                pads.append(_Pad(side, logical, poly))

        elif layer_type == "mask":
            for poly in getattr(layer, "polygons", []):
                area = _poly_area_mm2(poly)
                if area < mask_min_area_mm2:
                    continue
                masks.append(_MaskOpening(side, logical, poly))

    if not pads:
        viol = Violation(
            severity="info",
            message="No pad like copper features detected to estimate solder mask expansion.",
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

    min_expansion = math.inf
    min_loc: Optional[ViolationLocation] = None
    has_any_match = False

    for pad in pads:
        pad_w = pad.max_x - pad.min_x
        pad_h = pad.max_y - pad.min_y

        best_expansion_for_pad = math.inf

        for m in masks:
            if pad.side and m.side and str(pad.side).lower() != str(m.side).lower():
                continue

            if (
                m.max_x < pad.min_x - mask_search_inflate_mm
                or m.min_x > pad.max_x + mask_search_inflate_mm
                or m.max_y < pad.min_y - mask_search_inflate_mm
                or m.min_y > pad.max_y + mask_search_inflate_mm
            ):
                continue

            if not (m.min_x - mask_search_inflate_mm <= pad.cx <= m.max_x + mask_search_inflate_mm):
                continue
            if not (m.min_y - mask_search_inflate_mm <= pad.cy <= m.max_y + mask_search_inflate_mm):
                continue

            mask_w = m.max_x - m.min_x
            mask_h = m.max_y - m.min_y

            dx = mask_w - pad_w
            dy = mask_h - pad_h
            expansion = 0.5 * min(dx, dy)

            if expansion < best_expansion_for_pad:
                best_expansion_for_pad = expansion

        if best_expansion_for_pad is math.inf:
            continue

        has_any_match = True

        if best_expansion_for_pad < min_expansion:
            min_expansion = best_expansion_for_pad
            min_loc = ViolationLocation(
                layer=pad.layer,
                x_mm=pad.cx,
                y_mm=pad.cy,
                notes="Pad with smallest estimated solder mask expansion.",
            )

    if not has_any_match or not math.isfinite(min_expansion):
        viol = Violation(
            severity="info",
            message="Could not match solder mask openings to pads to estimate expansion; check mask polarity or Gerber conventions.",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            severity=ctx.check_def.severity or ctx.check_def.severity_default,
            status="warning",
            score=50.0,
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

    measured = float(min_expansion)

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
        f"Minimum solder mask expansion is {measured:.3f} mm "
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
