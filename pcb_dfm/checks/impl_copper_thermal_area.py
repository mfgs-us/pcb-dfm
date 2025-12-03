from __future__ import annotations

from typing import Optional, List

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..results import CheckResult, Violation, ViolationLocation


def _poly_area_mm2(poly) -> float:
    """
    Best-effort polygon area in mm^2, consistent with other checks.
    """
    if hasattr(poly, "area_mm2"):
        return float(poly.area_mm2)

    if hasattr(poly, "area"):
        try:
            return float(poly.area())
        except TypeError:
            try:
                return float(poly.area)
            except TypeError:
                pass

    # Fallback: bbox-based approximation
    b = poly.bounds()
    try:
        width = float(b.max_x - b.min_x)
        height = float(b.max_y - b.min_y)
        return max(0.0, width * height)
    except Exception:
        return 0.0


def _get_board_dims_mm(geom) -> Optional[tuple[float, float]]:
    """
    Try to get board width/height (mm) from geometry.
    Fallback to outline layer bounding box if needed.
    """
    board = getattr(geom, "board", None)
    if board is not None:
        w = getattr(board, "width_mm", None)
        h = getattr(board, "height_mm", None)
        if w is not None and h is not None:
            return float(w), float(h)

    # Fallback: outline polygons
    min_x = None
    max_x = None
    min_y = None
    max_y = None

    for layer in getattr(geom, "layers", []):
        layer_type = getattr(layer, "layer_type", getattr(layer, "type", None))
        if layer_type not in ("outline", "board_outline"):
            continue
        for poly in getattr(layer, "polygons", []):
            b = poly.bounds()
            xs = [float(b.min_x), float(b.max_x)]
            ys = [float(b.min_y), float(b.max_y)]
            if min_x is None:
                min_x, max_x = min(xs), max(xs)
                min_y, max_y = min(ys), max(ys)
            else:
                min_x = min(min_x, *xs)
                max_x = max(max_x, *xs)
                min_y = min(min_y, *ys)
                max_y = max(max_y, *ys)

    if min_x is None or max_x is None or min_y is None or max_y is None:
        return None

    return max(0.0, max_x - min_x), max(0.0, max_y - min_y)


@register_check("copper_thermal_area")
def run_copper_thermal_area(ctx: CheckContext) -> CheckResult:
    """
    Estimate how much copper plane area you have available for thermal spreading.

    Heuristic:
    - For each copper layer, compute copper area / board area.
    - Use max coverage across copper layers as the metric in %.
    """

    metric_cfg = ctx.check_def.metric or {}
    target_raw = metric_cfg.get("target", {}) or {}
    limits_raw = metric_cfg.get("limits", {}) or {}

    # Metric is ratio in percent.
    units = metric_cfg.get("units", "%")

    if isinstance(target_raw, dict):
        recommended_min_pct = float(target_raw.get("min", 30.0))
    else:
        recommended_min_pct = float(target_raw or 30.0)

    if isinstance(limits_raw, dict):
        absolute_min_pct = float(limits_raw.get("min", 15.0))
    else:
        absolute_min_pct = float(limits_raw or 15.0)

    geom = ctx.geometry

    dims = _get_board_dims_mm(geom)
    if dims is None:
        viol = Violation(
            severity="warning",
            message="Could not determine board outline to estimate copper thermal area.",
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
                "kind": "ratio",
                "units": units,
                "measured_value": None,
                "target": recommended_min_pct,
                "limit_low": absolute_min_pct,
                "limit_high": None,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    board_w_mm, board_h_mm = dims
    board_area_mm2 = max(1e-6, board_w_mm * board_h_mm)

    best_pct = 0.0
    best_layer_name: Optional[str] = None

    for layer in getattr(geom, "layers", []):
        layer_type = getattr(layer, "layer_type", getattr(layer, "type", None))
        if layer_type != "copper":
            continue

        logical = getattr(layer, "logical_layer", getattr(layer, "name", None))

        total_area = 0.0
        for poly in getattr(layer, "polygons", []):
            total_area += _poly_area_mm2(poly)

        # Clamp to board area so we don't exceed 100% due to overlaps, etc.
        total_area = min(total_area, board_area_mm2)
        coverage_pct = (total_area / board_area_mm2) * 100.0

        if coverage_pct > best_pct:
            best_pct = coverage_pct
            best_layer_name = logical

    # No copper at all
    if best_layer_name is None:
        viol = Violation(
            severity="warning",
            message="No copper layers found to evaluate thermal copper area.",
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
                "kind": "ratio",
                "units": units,
                "measured_value": 0.0,
                "target": recommended_min_pct,
                "limit_low": absolute_min_pct,
                "limit_high": None,
                "margin_to_limit": -absolute_min_pct,
            },
            violations=[viol],
        )

    measured = float(best_pct)

    # Simple scoring
    if measured >= recommended_min_pct:
        status = "pass"
        severity = ctx.check_def.severity or ctx.check_def.severity_default
        score = 100.0
    elif measured < absolute_min_pct:
        status = "fail"
        severity = "error"
        score = 0.0
    else:
        status = "warning"
        severity = "warning"
        span = max(1e-6, recommended_min_pct - absolute_min_pct)
        frac = (measured - absolute_min_pct) / span
        score = max(0.0, min(100.0, 60.0 + 40.0 * max(0.0, frac)))

    margin_to_limit = measured - absolute_min_pct

    loc = ViolationLocation(
        layer=best_layer_name,
        x_mm=board_w_mm / 2.0,
        y_mm=board_h_mm / 2.0,
        width_mm=board_w_mm,
        height_mm=board_h_mm,
        notes="Layer with highest plane-like copper coverage.",
    )

    msg = (
        f"Maximum copper coverage on any layer is {measured:.1f}% "
        f"(recommended >= {recommended_min_pct:.1f}%, absolute >= {absolute_min_pct:.1f}%)."
    )

    violations: List[Violation] = []
    if status != "pass":
        violations.append(
            Violation(
                severity=severity,
                message=msg,
                location=loc,
            )
        )
    else:
        violations.append(
            Violation(
                severity="info",
                message=msg,
                location=loc,
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
            "units": units,
            "measured_value": measured,
            "target": recommended_min_pct,
            "limit_low": absolute_min_pct,
            "limit_high": None,
            "margin_to_limit": margin_to_limit,
        },
        violations=violations,
    )
