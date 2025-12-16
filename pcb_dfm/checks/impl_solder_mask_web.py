from __future__ import annotations

import math
from collections import defaultdict
from math import floor
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


def _bbox_distance_mm(b1, b2) -> float:
    dx = max(0.0, max(b1.min_x - b2.max_x, b2.min_x - b1.max_x))
    dy = max(0.0, max(b1.min_y - b2.max_y, b2.min_y - b1.max_y))
    if dx == 0.0 and dy == 0.0:
        return 0.0
    return math.hypot(dx, dy)


def _cell_key(x: float, y: float, cell: float):
    return (int(floor(x / cell)), int(floor(y / cell)))


@register_check("solder_mask_web")
def run_solder_mask_web(ctx: CheckContext) -> CheckResult:
    """
    Minimum mask web width between adjacent mask openings.

    Internal geometry in mm. Metric reported in mm.
    """
    metric_cfg = ctx.check_def.metric or {}
    target_raw = metric_cfg.get("target", {}) or {}
    limits_raw = metric_cfg.get("limits", {}) or {}

    units_raw = (metric_cfg.get("units") or "mm").lower()
    source_is_um = units_raw in ("um", "micron", "microns")

    if isinstance(target_raw, dict):
        raw_target_min = target_raw.get("min", 75.0 if source_is_um else 0.075)
    else:
        raw_target_min = 75.0 if source_is_um else 0.075

    if isinstance(limits_raw, dict):
        raw_abs_min = limits_raw.get("min", 50.0 if source_is_um else 0.05)
    else:
        raw_abs_min = 50.0 if source_is_um else 0.05

    scale = 0.001 if source_is_um else 1.0
    recommended_min = float(raw_target_min) * scale
    absolute_min = float(raw_abs_min) * scale

    raw_cfg = getattr(ctx.check_def, "raw", None) or {}
    opening_min_area_mm2 = float(raw_cfg.get("opening_min_area_mm2", 0.02))
    opening_min_short_dim_mm = float(raw_cfg.get("opening_min_short_dim_mm", 0.1))
    spacing_epsilon_mm = float(raw_cfg.get("spacing_epsilon_mm", 0.001))

    geom = ctx.geometry

    class _Opening:
        __slots__ = ("side", "layer", "min_x", "max_x", "min_y", "max_y", "cx", "cy")

        def __init__(self, side, layer, poly):
            self.side = side
            self.layer = layer
            b = poly.bounds()
            self.min_x = b.min_x
            self.max_x = b.max_x
            self.min_y = b.min_y
            self.max_y = b.max_y
            self.cx = 0.5 * (b.min_x + b.max_x)
            self.cy = 0.5 * (b.min_y + b.max_y)

    openings: List[_Opening] = []

    for layer in getattr(geom, "layers", []):
        layer_type = getattr(layer, "layer_type", getattr(layer, "type", None))
        if layer_type != "mask":
            continue

        side = getattr(layer, "side", None)
        logical = getattr(layer, "logical_layer", getattr(layer, "name", None))

        for poly in getattr(layer, "polygons", []):
            area = _poly_area_mm2(poly)
            if area < opening_min_area_mm2:
                continue

            b = poly.bounds()
            w = max(0.0, b.max_x - b.min_x)
            h = max(0.0, b.max_y - b.min_y)
            short_dim = min(w, h)
            if short_dim < opening_min_short_dim_mm:
                continue

            openings.append(_Opening(side, logical, poly))

    if len(openings) < 2:
        viol = Violation(
            severity="info",
            message="Too few mask openings to estimate solder mask web width.",
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

    cell = max(recommended_min, 0.25)  # mm
    grid = defaultdict(list)

    for idx, o in enumerate(openings):
        grid[_cell_key(o.cx, o.cy, cell)].append(idx)

    min_spacing = math.inf
    min_loc: Optional[ViolationLocation] = None

    for i, oi in enumerate(openings):
        ci, cj = _cell_key(oi.cx, oi.cy, cell)

        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for j in grid.get((ci + di, cj + dj), []):
                    if j <= i:
                        continue
                    oj = openings[j]

                    if oi.side and oj.side and str(oi.side).lower() != str(oj.side).lower():
                        continue

                    class _B:
                        __slots__ = ("min_x", "max_x", "min_y", "max_y")

                        def __init__(self, min_x, max_x, min_y, max_y):
                            self.min_x = min_x
                            self.max_x = max_x
                            self.min_y = min_y
                            self.max_y = max_y

                    bi = _B(oi.min_x, oi.max_x, oi.min_y, oi.max_y)
                    bj = _B(oj.min_x, oj.max_x, oj.min_y, oj.max_y)

                    d = _bbox_distance_mm(bi, bj)
                    if d < spacing_epsilon_mm:
                        continue

                    if d < min_spacing:
                        min_spacing = d
                        cx = 0.5 * (oi.cx + oj.cx)
                        cy = 0.5 * (oi.cy + oj.cy)
                        min_loc = ViolationLocation(
                            layer=oi.layer or oj.layer,
                            x_mm=cx,
                            y_mm=cy,
                            notes="Narrowest solder mask web between adjacent openings.",
                        )

    if not math.isfinite(min_spacing):
        viol = Violation(
            severity="info",
            message="No nonzero mask web spacing detected; mask openings appear either merged or isolated.",
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

    measured = float(min_spacing)

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
        f"Minimum solder mask web width is {measured:.3f} mm "
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
