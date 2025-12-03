from __future__ import annotations

import math
from typing import List, Optional

from ..results import CheckResult, Violation, ViolationLocation
from ..engine.context import CheckContext
from ..engine.check_runner import register_check
from ..ingest import GerberFileInfo

# Use the same gerber backend as min_drill_size
try:
    import gerber
except Exception:
    gerber = None

_INCH_TO_MM = 25.4


def _poly_area_mm2(poly) -> float:
    """
    Best effort polygon area in mm^2.
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

    # Fallback to bbox area
    try:
        b = poly.bounds()
        width = float(b.max_x - b.min_x)
        height = float(b.max_y - b.min_y)
        return max(0.0, width * height)
    except Exception:
        return 0.0


def _bbox_contains_point(poly, x_mm: float, y_mm: float, margin_mm: float = 0.0) -> bool:
    """
    Quick bbox containment test with optional margin.
    """
    b = poly.bounds()
    min_x = float(b.min_x) - margin_mm
    max_x = float(b.max_x) + margin_mm
    min_y = float(b.min_y) - margin_mm
    max_y = float(b.max_y) + margin_mm
    return (min_x <= x_mm <= max_x) and (min_y <= y_mm <= max_y)


def _extract_drill_hits_mm(path: str) -> List[dict]:
    """
    Use gerber.read on a drill file and extract hits as mm.

    Returns list of dicts with keys:
      - x_mm
      - y_mm
      - diameter_mm
      - plated (bool, default True)
    """
    if gerber is None:
        return []

    try:
        drill_layer = gerber.read(path)
    except Exception:
        return []

    # Normalize to inch if possible
    try:
        drill_layer.to_inch()
    except Exception:
        pass

    hits_out: List[dict] = []

    hits = getattr(drill_layer, "hits", None)
    if hits is None:
        return hits_out

    for hit in hits:
        x_in = y_in = None
        d_in = None
        plated = True

        # Newer style objects: hit.x, hit.y, hit.tool.diameter
        try:
            if hasattr(hit, "x") and hasattr(hit, "y"):
                x_in = float(hit.x)
                y_in = float(hit.y)
            elif hasattr(hit, "position"):
                px, py = hit.position  # type: ignore[attr-defined]
                x_in = float(px)
                y_in = float(py)

            tool = getattr(hit, "tool", None)
            if tool is not None:
                d_in = getattr(tool, "diameter", None)
                if d_in is None:
                    d_in = getattr(tool, "size", None)
                plated_attr = getattr(tool, "plated", None)
                if plated_attr is not None:
                    plated = bool(plated_attr)
        except Exception:
            x_in = y_in = d_in = None

        # Older formats: (tool, (x, y))
        if x_in is None or y_in is None or d_in is None:
            try:
                tool, (px, py) = hit  # type: ignore[misc]
                x_in = float(px)
                y_in = float(py)
                d_in = getattr(tool, "diameter", None)
                if d_in is None:
                    d_in = getattr(tool, "size", None)
                plated_attr = getattr(tool, "plated", None)
                if plated_attr is not None:
                    plated = bool(plated_attr)
            except Exception:
                continue

        try:
            d_in_float = float(d_in)
        except Exception:
            continue

        hits_out.append(
            {
                "x_mm": x_in * _INCH_TO_MM,
                "y_mm": y_in * _INCH_TO_MM,
                "diameter_mm": d_in_float * _INCH_TO_MM,
                "plated": plated,
            }
        )

    return hits_out


@register_check("via_in_pad_thermal_balance")
def run_via_in_pad_thermal_balance(ctx: CheckContext) -> CheckResult:
    """
    Estimate thermal balance risk for vias placed in copper pads.

    Drill source: same as min_drill_size / drill_to_drill_spacing
    - ctx.ingest.files where layer_type == "drill"
    - parsed via gerber.read(...).hits

    For each plated drill:
      - find copper polygons whose bbox contains the drill center
      - treat those polygons as pads
      - compute via area / pad area
      - metric is worst ratio in percent
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", "%")
    target_cfg = metric_cfg.get("target", {}) or {}
    limits_cfg = metric_cfg.get("limits", {}) or {}

    # We treat metric as percent 0..100
    recommended_max_pct = float(target_cfg.get("max", 20.0)) if isinstance(target_cfg, dict) else float(target_cfg or 20.0)
    absolute_max_pct = float(limits_cfg.get("max", 40.0)) if isinstance(limits_cfg, dict) else float(limits_cfg or 40.0)

    raw_cfg = getattr(ctx.check_def, "raw", None) or {}
    min_pad_area_mm2 = float(raw_cfg.get("min_pad_area_mm2", 0.05))
    max_pad_area_mm2 = float(raw_cfg.get("max_pad_area_mm2", 10.0))
    bbox_margin_mm = float(raw_cfg.get("bbox_margin_mm", 0.02))

    # 1) Collect drills: same source as min_drill_size
    drill_files: List[GerberFileInfo] = [
        f for f in ctx.ingest.files if f.layer_type == "drill"
    ]

    drill_hits: List[dict] = []
    if gerber is not None:
        for info in drill_files:
            drill_hits.extend(_extract_drill_hits_mm(str(info.path)))

    if not drill_hits:
        viol = Violation(
            severity="info",
            message="No drill/via hits found to evaluate via in pad thermal balance.",
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
                "units": units,
                "measured_value": None,
                "target": recommended_max_pct,
                "limit_low": None,
                "limit_high": absolute_max_pct,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    # 2) Collect copper polygons that look pad like
    geom = ctx.geometry
    copper_polys: List[tuple[str, object]] = []  # (layer_name, poly)

    for layer in getattr(geom, "layers", []):
        layer_type = getattr(layer, "layer_type", getattr(layer, "type", None))
        if layer_type != "copper":
            continue
        logical = getattr(layer, "logical_layer", getattr(layer, "name", None))

        for poly in getattr(layer, "polygons", []):
            area = _poly_area_mm2(poly)
            if area < min_pad_area_mm2 or area > max_pad_area_mm2:
                continue
            copper_polys.append((logical, poly))

    if not copper_polys:
        viol = Violation(
            severity="info",
            message="No suitable copper pad like features found to evaluate via in pad thermal balance.",
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
                "units": units,
                "measured_value": None,
                "target": recommended_max_pct,
                "limit_low": None,
                "limit_high": absolute_max_pct,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    # 3) Scan vias in pads and track worst via area / pad area
    worst_ratio_pct = 0.0
    worst_loc: Optional[ViolationLocation] = None

    for hit in drill_hits:
        if not hit.get("plated", True):
            continue

        x_mm = float(hit["x_mm"])
        y_mm = float(hit["y_mm"])
        d_mm = float(hit["diameter_mm"])
        if d_mm <= 0.0:
            continue

        via_area = math.pi * (d_mm * 0.5) ** 2

        for layer_name, poly in copper_polys:
            if not _bbox_contains_point(poly, x_mm, y_mm, margin_mm=bbox_margin_mm):
                continue

            pad_area = _poly_area_mm2(poly)
            if pad_area <= 0.0:
                continue

            ratio = via_area / pad_area
            ratio_pct = ratio * 100.0

            if ratio_pct > worst_ratio_pct:
                worst_ratio_pct = ratio_pct
                worst_loc = ViolationLocation(
                    layer=layer_name,
                    x_mm=x_mm,
                    y_mm=y_mm,
                    width_mm=None,
                    height_mm=None,
                    net=None,
                    component=None,
                    notes="Via in pad with highest via to pad area ratio.",
                )

    if worst_loc is None:
        viol = Violation(
            severity="info",
            message="No via in pad configurations detected based on simple geometry heuristics.",
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
                "units": units,
                "measured_value": 0.0,
                "target": recommended_max_pct,
                "limit_low": None,
                "limit_high": absolute_max_pct,
                "margin_to_limit": absolute_max_pct,
            },
            violations=[viol],
        )

    measured = float(worst_ratio_pct)

    # Status and score in percent space
    if measured <= recommended_max_pct:
        status = "pass"
        severity = ctx.check_def.severity or ctx.check_def.severity_default
        score = 100.0
    elif measured > absolute_max_pct:
        status = "fail"
        severity = "error"
        score = 0.0
    else:
        status = "warning"
        severity = "warning"
        span = max(1e-6, absolute_max_pct - recommended_max_pct)
        frac = (measured - recommended_max_pct) / span
        # 60-100 range in warning band
        score = max(0.0, min(100.0, 100.0 - 40.0 * max(0.0, frac)))

    margin_to_limit = absolute_max_pct - measured

    msg = (
        f"Worst via in pad area ratio is {measured:.1f}% "
        f"(recommended <= {recommended_max_pct:.1f}%, absolute <= {absolute_max_pct:.1f}%)."
    )

    violations: List[Violation] = []
    if status != "pass":
        violations.append(
            Violation(
                severity=severity,
                message=msg,
                location=worst_loc,
            )
        )
    else:
        violations.append(
            Violation(
                severity="info",
                message=msg,
                location=worst_loc,
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
            "target": recommended_max_pct,
            "limit_low": None,
            "limit_high": absolute_max_pct,
            "margin_to_limit": margin_to_limit,
        },
        violations=violations,
    )
