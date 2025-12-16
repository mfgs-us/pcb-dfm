from __future__ import annotations

import math
from collections import defaultdict
from math import floor
from typing import Dict, List, Optional, Tuple

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


def _cell_key(x: float, y: float, cell: float) -> Tuple[int, int]:
    return (int(floor(x / cell)), int(floor(y / cell)))


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


def _poly_bbox_dims_mm(poly) -> Tuple[float, float, float, float, float, float]:
    """
    Returns (min_x, min_y, max_x, max_y, w, h) in mm.
    """
    b = poly.bounds()
    min_x = float(b.min_x)
    min_y = float(b.min_y)
    max_x = float(b.max_x)
    max_y = float(b.max_y)
    w = max(0.0, max_x - min_x)
    h = max(0.0, max_y - min_y)
    return min_x, min_y, max_x, max_y, w, h


def _bbox_center_mm(poly) -> Tuple[float, float]:
    min_x, min_y, max_x, max_y, _, _ = _poly_bbox_dims_mm(poly)
    return (0.5 * (min_x + max_x), 0.5 * (min_y + max_y))


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

    Upgraded heuristics:
      - Filter drill hits to "via-like" plated holes (diameter range)
      - Identify pad-like copper polygons by area + aspect ratio + max dimension
      - Consider a via "in pad" only if:
          (a) via center is within pad bbox (with margin)
          (b) via center is close to pad bbox centroid (to avoid matching pours / large regions)
      - If multiple pads match, choose the best candidate (smallest pad area).
      - Metric is worst ratio in percent: (via_area / pad_area) * 100
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", "%")
    target_cfg = metric_cfg.get("target", {}) or {}
    limits_cfg = metric_cfg.get("limits", {}) or {}

    # We treat metric as percent 0..100
    recommended_max_pct = (
        float(target_cfg.get("max", 20.0)) if isinstance(target_cfg, dict) else float(target_cfg or 20.0)
    )
    absolute_max_pct = (
        float(limits_cfg.get("max", 40.0)) if isinstance(limits_cfg, dict) else float(limits_cfg or 40.0)
    )

    raw_cfg = getattr(ctx.check_def, "raw", None) or {}

    # Pad-like copper selection
    min_pad_area_mm2 = float(raw_cfg.get("min_pad_area_mm2", 0.05))
    max_pad_area_mm2 = float(raw_cfg.get("max_pad_area_mm2", 10.0))
    max_pad_aspect = float(raw_cfg.get("max_pad_aspect", 4.0))
    max_pad_dim_mm = float(raw_cfg.get("max_pad_dim_mm", 6.0))

    # Via-in-pad detection tolerances
    bbox_margin_mm = float(raw_cfg.get("bbox_margin_mm", 0.02))
    # Via center must be near pad centroid, else likely matching a pour/region
    pad_center_max_offset_mm = float(raw_cfg.get("pad_center_max_offset_mm", 0.6))
    pad_center_max_offset_frac = float(raw_cfg.get("pad_center_max_offset_frac", 0.40))

    # Filter drills to "via-like" hits
    min_via_d_mm = float(raw_cfg.get("min_via_d_mm", 0.15))
    max_via_d_mm = float(raw_cfg.get("max_via_d_mm", 0.60))

    # 1) Collect drills
    drill_files: List[GerberFileInfo] = [f for f in ctx.ingest.files if f.layer_type == "drill"]

    drill_hits: List[dict] = []
    if gerber is not None:
        for info in drill_files:
            drill_hits.extend(_extract_drill_hits_mm(str(info.path)))

    # Filter to plated + via-like diameters
    via_hits: List[dict] = []
    for h in drill_hits:
        if not h.get("plated", True):
            continue
        d = float(h.get("diameter_mm", 0.0) or 0.0)
        if d <= 0.0:
            continue
        if d < min_via_d_mm or d > max_via_d_mm:
            continue
        via_hits.append(h)

    if not via_hits:
        viol = Violation(
            severity="info",
            message="No plated via-like drill hits found to evaluate via in pad thermal balance.",
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

    # 2) Collect pad-like copper polygons (all copper layers)
    geom = ctx.geometry
    copper_polys: List[tuple[str, object, object, float, float, float, float, float]] = []
    # (layer_name, poly, bbox, area, cx, cy, w, h)

    for layer in getattr(geom, "layers", []):
        layer_type = getattr(layer, "layer_type", getattr(layer, "type", None))
        if layer_type != "copper":
            continue

        logical = getattr(layer, "logical_layer", getattr(layer, "name", None)) or "Copper"

        for poly in getattr(layer, "polygons", []):
            area = _poly_area_mm2(poly)
            if area < min_pad_area_mm2 or area > max_pad_area_mm2:
                continue

            b = poly.bounds()
            min_x = float(b.min_x); max_x = float(b.max_x)
            min_y = float(b.min_y); max_y = float(b.max_y)
            w = max(0.0, max_x - min_x)
            h = max(0.0, max_y - min_y)

            if w <= 0.0 or h <= 0.0:
                continue

            long_dim = max(w, h)
            short_dim = min(w, h)
            if long_dim > max_pad_dim_mm:
                continue

            aspect = (long_dim / short_dim) if short_dim > 0.0 else 999.0
            if aspect > max_pad_aspect:
                continue

            cx = 0.5 * (min_x + max_x)
            cy = 0.5 * (min_y + max_y)
            copper_polys.append((logical, poly, b, area, cx, cy, w, h))

    if not copper_polys:
        viol = Violation(
            severity="info",
            message="No suitable pad-like copper features found to evaluate via in pad thermal balance.",
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

    # Grid index pad bboxes
    cell = max(max_via_d_mm + 2.0, 1.0)  # conservative: pads are small, 1-2mm typical
    grid: Dict[Tuple[int, int], List[int]] = defaultdict(list)

    for idx, (_lname, _poly, b, _area, _cx, _cy, _w, _h) in enumerate(copper_polys):
        ix0 = int(floor(b.min_x / cell))
        ix1 = int(floor(b.max_x / cell))
        iy0 = int(floor(b.min_y / cell))
        iy1 = int(floor(b.max_y / cell))
        for iy in range(iy0, iy1 + 1):
            for ix in range(ix0, ix1 + 1):
                grid[(ix, iy)].append(idx)

    # 3) For each via, find best-matching pad and compute via area / pad area
    worst_ratio_pct = 0.0
    worst_loc: Optional[ViolationLocation] = None

    for hit in via_hits:
        x_mm = float(hit["x_mm"])
        y_mm = float(hit["y_mm"])
        d_mm = float(hit["diameter_mm"])
        if d_mm <= 0.0:
            continue

        via_area = math.pi * (d_mm * 0.5) ** 2

        best_pad: Optional[tuple[str, object, object, float, float, float, float, float]] = None

        ci, cj = _cell_key(x_mm, y_mm, cell)

        # Search nearby cells (3x3 is generally enough because pads are small)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for idx in grid.get((ci + di, cj + dj), []):
                    layer_name, poly, b, pad_area, pcx, pcy, pw, ph = copper_polys[idx]

                    # bbox contains (with margin) using bbox directly
                    if not (
                        (b.min_x - bbox_margin_mm) <= x_mm <= (b.max_x + bbox_margin_mm)
                        and (b.min_y - bbox_margin_mm) <= y_mm <= (b.max_y + bbox_margin_mm)
                    ):
                        continue

                    local_scale = max(1e-9, min(pw, ph))
                    max_offset = max(pad_center_max_offset_mm, pad_center_max_offset_frac * local_scale)
                    if math.hypot(x_mm - pcx, y_mm - pcy) > max_offset:
                        continue

                    if best_pad is None or pad_area < best_pad[3]:
                        best_pad = (layer_name, poly, b, pad_area, pcx, pcy, pw, ph)

        if best_pad is None:
            continue

        layer_name, _, pad_area, _, _, _, _ = best_pad
        if pad_area <= 0.0:
            continue

        ratio_pct = (via_area / pad_area) * 100.0

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
                notes="Via-in-pad detected: worst via area to pad area ratio (via near pad centroid).",
            )

    if worst_loc is None:
        viol = Violation(
            severity="info",
            message="No via-in-pad configurations detected based on upgraded geometry heuristics.",
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
        f"Worst via-in-pad area ratio is {measured:.1f}% "
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
