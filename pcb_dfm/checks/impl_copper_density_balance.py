from __future__ import annotations

import math
from typing import List, Optional, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, Violation, ViolationLocation


def _compute_board_bounds_mm(ctx: CheckContext) -> Optional[Tuple[float, float, float, float]]:
    """
    Try to get board bounds from geometry; if not available, derive from all polygons.
    Returns (min_x, max_x, min_y, max_y) in mm, or None if no geometry.
    """
    geom = ctx.geometry

    # Preferred: a dedicated board bounds attribute if it exists
    for attr in ("board_bounds_mm", "board_bounds"):
        bb = getattr(geom, attr, None)
        if bb is not None and hasattr(bb, "min_x") and hasattr(bb, "max_x"):
            return float(bb.min_x), float(bb.max_x), float(bb.min_y), float(bb.max_y)

    # Fallback: derive from all polygons
    min_x = math.inf
    max_x = -math.inf
    min_y = math.inf
    max_y = -math.inf
    found = False

    for layer in getattr(geom, "layers", []):
        for poly in getattr(layer, "polygons", []):
            b = poly.bounds()
            min_x = min(min_x, b.min_x)
            max_x = max(max_x, b.max_x)
            min_y = min(min_y, b.min_y)
            max_y = max(max_y, b.max_y)
            found = True

    if not found or not math.isfinite(min_x) or not math.isfinite(max_x):
        return None

    return float(min_x), float(max_x), float(min_y), float(max_y)


def _poly_bbox_area_mm2(poly) -> float:
    b = poly.bounds()
    return max(0.0, (b.max_x - b.min_x) * (b.max_y - b.min_y))


def _poly_overlap_with_window(
    poly, wx_min: float, wx_max: float, wy_min: float, wy_max: float
) -> float:
    """
    Approximate overlap area between polygon and window using bounding boxes only.
    This is intentionally approximate but stable and fast.
    """
    b = poly.bounds()
    ix_min = max(b.min_x, wx_min)
    ix_max = min(b.max_x, wx_max)
    iy_min = max(b.min_y, wy_min)
    iy_max = min(b.max_y, wy_max)

    if ix_max <= ix_min or iy_max <= iy_min:
        return 0.0

    return max(0.0, (ix_max - ix_min) * (iy_max - iy_min))


@register_check("copper_density_balance")
def run_copper_density_balance(ctx: CheckContext) -> CheckResult:
    """
    Compute local copper density balance using a sliding window approximation.

    - Tile the board with WxW windows (default 5 mm).
    - For each window, approximate copper area via polygon bbox overlap.
    - Compute density per window (0..1).
    - For adjacent windows (right and down neighbors), compute density delta.
    - Report the maximum delta as the metric.

    This is an approximation, but good enough to highlight gross copper imbalance.
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", "percent")

    limits = ctx.check_def.limits or {}
    recommended_max_delta = float(limits.get("recommended_max_delta", 20.0))  # percent
    absolute_max_delta = float(limits.get("absolute_max_delta", 30.0))        # percent

    raw_cfg = ctx.check_def.raw or {}
    window_size_mm = float(raw_cfg.get("window_size_mm", 5.0))
    min_window_copper_area_mm2 = float(raw_cfg.get("min_window_copper_area_mm2", 0.2))

    geom = ctx.geometry

    # Collect copper layers and polygons
    copper_layers = []
    for layer in getattr(geom, "layers", []):
        layer_type = getattr(layer, "layer_type", getattr(layer, "type", None))
        if layer_type == "copper":
            copper_layers.append(layer)

    if not copper_layers:
        viol = Violation(
            severity="info",
            message="No copper layers found; skipping copper density balance check.",
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
                "kind": "ratio",
                "units": units,
                "measured_value": None,
                "target": recommended_max_delta,
                "limit_low": None,
                "limit_high": absolute_max_delta,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    board_bounds = _compute_board_bounds_mm(ctx)
    if board_bounds is None:
        viol = Violation(
            severity="warning",
            message="Could not determine board bounds; cannot compute density windows.",
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
                "kind": "ratio",
                "units": units,
                "measured_value": None,
                "target": recommended_max_delta,
                "limit_low": None,
                "limit_high": absolute_max_delta,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    bx_min, bx_max, by_min, by_max = board_bounds
    board_w = max(0.0, bx_max - bx_min)
    board_h = max(0.0, by_max - by_min)
    if board_w <= 0.0 or board_h <= 0.0:
        viol = Violation(
            severity="warning",
            message="Board bounds are degenerate; cannot compute density.",
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
                "kind": "ratio",
                "units": units,
                "measured_value": None,
                "target": recommended_max_delta,
                "limit_low": None,
                "limit_high": absolute_max_delta,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    # Number of windows along X/Y
    nx = max(1, math.ceil(board_w / window_size_mm))
    ny = max(1, math.ceil(board_h / window_size_mm))

    window_density = [[0.0 for _ in range(nx)] for _ in range(ny)]

    # Pre-collect all copper polygons
    copper_polys = []
    for layer in copper_layers:
        for poly in getattr(layer, "polygons", []):
            copper_polys.append(poly)

    if not copper_polys:
        viol = Violation(
            severity="info",
            message="No copper polygons found; copper density is effectively 0 everywhere.",
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
                "kind": "ratio",
                "units": units,
                "measured_value": 0.0,
                "target": recommended_max_delta,
                "limit_low": None,
                "limit_high": absolute_max_delta,
                "margin_to_limit": absolute_max_delta,
            },
            violations=[viol],
        )

    # Compute density per window
    for iy in range(ny):
        for ix in range(nx):
            wx_min = bx_min + ix * window_size_mm
            wx_max = min(bx_min + (ix + 1) * window_size_mm, bx_max)
            wy_min = by_min + iy * window_size_mm
            wy_max = min(by_min + (iy + 1) * window_size_mm, by_max)

            w_area = max(0.0, (wx_max - wx_min) * (wy_max - wy_min))
            if w_area <= 0.0:
                window_density[iy][ix] = 0.0
                continue

            copper_area = 0.0
            for poly in copper_polys:
                copper_area += _poly_overlap_with_window(poly, wx_min, wx_max, wy_min, wy_max)

            if copper_area < min_window_copper_area_mm2:
                density = 0.0
            else:
                density = max(0.0, min(1.0, copper_area / w_area))

            window_density[iy][ix] = density

    # Compute max density delta between neighbors
    max_delta = 0.0
    worst_center_x = None
    worst_center_y = None

    def _record_delta(d: float, x_center: float, y_center: float):
        nonlocal max_delta, worst_center_x, worst_center_y
        if d > max_delta:
            max_delta = d
            worst_center_x = x_center
            worst_center_y = y_center

    for iy in range(ny):
        for ix in range(nx):
            d_here = window_density[iy][ix]

            # Compute actual window bounds for (ix, iy) - matches density calc
            wx0 = bx_min + ix * window_size_mm
            wx1 = min(bx_min + (ix + 1) * window_size_mm, bx_max)
            wy0 = by_min + iy * window_size_mm
            wy1 = min(by_min + (iy + 1) * window_size_mm, by_max)

            # right neighbor
            if ix + 1 < nx:
                d_r = window_density[iy][ix + 1]
                delta = abs(d_here - d_r)

                # shared vertical boundary x = wx1, y midpoint of this window
                cx = wx1
                cy = 0.5 * (wy0 + wy1)
                _record_delta(delta, cx, cy)

            # down neighbor
            if iy + 1 < ny:
                d_d = window_density[iy + 1][ix]
                delta = abs(d_here - d_d)

                # shared horizontal boundary y = wy1, x midpoint of this window
                cx = 0.5 * (wx0 + wx1)
                cy = wy1
                _record_delta(delta, cx, cy)


    # Convert to percent
    max_delta_percent = max_delta * 100.0

    if worst_center_x is None or worst_center_y is None:
        # No neighbors or trivial single window
        viol = Violation(
            severity="info",
            message="Board too small for meaningful density balance; treating as uniform.",
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
                "kind": "ratio",
                "units": units,
                "measured_value": 0.0,
                "target": recommended_max_delta,
                "limit_low": None,
                "limit_high": absolute_max_delta,
                "margin_to_limit": absolute_max_delta,
            },
            violations=[viol],
        )

    # Status and score
    if max_delta_percent <= recommended_max_delta:
        status = "pass"
        severity = ctx.check_def.severity or "error"
        score = 100.0
    elif max_delta_percent <= absolute_max_delta:
        status = "warning"
        severity = "warning"
        # Linear falloff between recommended and absolute
        span = max(1e-6, absolute_max_delta - recommended_max_delta)
        score = max(
            0.0,
            min(
                100.0,
                100.0 * (absolute_max_delta - max_delta_percent) / span,
            ),
        )
    else:
        status = "fail"
        severity = "error"
        score = 0.0

    margin_to_limit = float(absolute_max_delta - max_delta_percent)

    violations: List[Violation] = []
    if status != "pass":
        msg = (
            f"Maximum local copper density delta between adjacent regions is "
            f"{max_delta_percent:.1f}% (recommended <= {recommended_max_delta:.1f}%, "
            f"absolute <= {absolute_max_delta:.1f}%)."
        )
        violations.append(
            Violation(
                severity=severity,
                message=msg,
                location=ViolationLocation(
                    layer=None,
                    x_mm=worst_center_x,
                    y_mm=worst_center_y,
                ),
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
            "kind": "ratio",
            "units": units,
            "measured_value": max_delta_percent,
            "target": recommended_max_delta,
            "limit_low": None,
            "limit_high": absolute_max_delta,
            "margin_to_limit": margin_to_limit,
        },
        violations=violations,
    )
