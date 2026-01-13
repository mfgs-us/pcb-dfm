from __future__ import annotations

import math
from typing import List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, Violation, ViolationLocation, MetricResult


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


def _bboxes_overlap(b1, b2) -> bool:
    if b1.max_x < b2.min_x or b2.max_x < b1.min_x:
        return False
    if b1.max_y < b2.min_y or b2.max_y < b1.min_y:
        return False
    return True


@register_check("silkscreen_over_mask_defined_pads")
def run_silkscreen_over_mask_defined_pads(ctx: CheckContext) -> CheckResult:
    """
    Heuristic detection of silkscreen overlapping pad like copper regions.

    We do not yet model mask polarity in detail, so this approximates
    "silkscreen on exposed copper" as any bbox overlap between silkscreen
    polygons and pad like copper polygons on the same side.
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", "mm2")  # area like units, but we mostly use count
    target_cfg = metric_cfg.get("target", {}) or {}
    limits_cfg = metric_cfg.get("limits", {}) or {}

    target_max = float(target_cfg.get("max", 0.0))
    limit_max = float(limits_cfg.get("max", 0.0))

    raw_cfg = getattr(ctx.check_def, "raw", None) or {}
    pad_min_area_mm2 = float(raw_cfg.get("pad_min_area_mm2", 0.02))
    pad_max_area_mm2 = float(raw_cfg.get("pad_max_area_mm2", 4.0))
    pad_max_aspect_ratio = float(raw_cfg.get("pad_max_aspect_ratio", 10.0))
    silk_min_area_mm2 = float(raw_cfg.get("silk_min_area_mm2", 0.01))

    geom = ctx.geometry

    class _BBox:
        __slots__ = ("min_x", "max_x", "min_y", "max_y")

        def __init__(self, min_x, max_x, min_y, max_y):
            self.min_x = min_x
            self.max_x = max_x
            self.min_y = min_y
            self.max_y = max_y

    class _Pad:
        __slots__ = ("side", "layer", "bbox")

        def __init__(self, side, layer, poly):
            b = poly.bounds()
            self.side = side
            self.layer = layer
            self.bbox = _BBox(b.min_x, b.max_x, b.min_y, b.max_y)

    class _Silk:
        __slots__ = ("side", "layer", "bbox", "cx", "cy")

        def __init__(self, side, layer, poly):
            b = poly.bounds()
            self.side = side
            self.layer = layer
            self.bbox = _BBox(b.min_x, b.max_x, b.min_y, b.max_y)
            self.cx = 0.5 * (b.min_x + b.max_x)
            self.cy = 0.5 * (b.min_y + b.max_y)

    pads: List[_Pad] = []
    silks: List[_Silk] = []

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

        elif layer_type in ("silkscreen", "silk"):
            for poly in getattr(layer, "polygons", []):
                area = _poly_area_mm2(poly)
                if area < silk_min_area_mm2:
                    continue
                silks.append(_Silk(side, logical, poly))

    if not pads or not silks:
        viol = Violation(
            severity="info",
            message="No overlapping silkscreen and pad like features detected (no silkscreen or pads found).",
            location=None,
        )
        return CheckResult(
            check_id=ctx.check_def.id,
            name=ctx.check_def.name,
            category_id=ctx.check_def.category_id,
            status="pass",
            severity="info",  # Default value, will be overridden by finalize()
            score=100.0,
            metric=MetricResult(
                kind="count",
                units="overlaps",
                measured_value=0,
                target=target_max,
                limit_high=target_max,
                margin_to_limit=0,
            ),
            violations=[viol],
        ).finalize()

    overlap_count = 0
    first_loc: Optional[ViolationLocation] = None

    for s in silks:
        for p in pads:
            if s.side and p.side and str(s.side).lower() != str(p.side).lower():
                continue
            if _bboxes_overlap(s.bbox, p.bbox):
                overlap_count += 1
                if first_loc is None:
                    first_loc = ViolationLocation(
                        layer=s.layer,
                        x_mm=s.cx,
                        y_mm=s.cy,
                        notes="Approximate location where silkscreen overlaps pad like copper.",
                    )

    measured = float(overlap_count)

    if measured <= target_max:
        status = "pass"
        severity = ctx.check_def.severity or ctx.check_def.severity_default
        score = 100.0
    elif measured <= limit_max:
        status = "warning"
        severity = "error"
        # simple scoring from 100 down to 60 as we approach limit_max
        span = max(1.0, limit_max - target_max)
        frac = max(0.0, min(1.0, (measured - target_max) / span))
        score = max(0.0, min(100.0, 100.0 - 40.0 * frac))
    else:
        status = "warning"
        severity = "error"
        score = 0.0

    margin_to_limit = float(limit_max - measured)

    msg = None
    if measured == 0:
        msg = "No silkscreen overlaps detected on pad like copper regions."
    else:
        msg = f"Detected {overlap_count} silkscreen region(s) overlapping pad like copper; silkscreen may need clipping in these areas."

    violations: List[Violation] = []
    if measured > 0:
        violations.append(
            Violation(
                severity=severity,
                message=msg,
                location=first_loc,
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
        severity="info",  # Default value, will be overridden by finalize()
        status=status,
        score=score,
        metric=MetricResult(
            kind="count",
            units="overlaps",
            measured_value=measured,
            target=target_max,
            limit_high=limit_max,
            margin_to_limit=margin_to_limit,
        ),
        violations=violations,
    ).finalize()
