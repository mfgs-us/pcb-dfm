from __future__ import annotations

from typing import List, Optional, Tuple

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


@register_check("thermal_relief_spoke_width")
def run_thermal_relief_spoke_width(ctx: CheckContext) -> CheckResult:
    """
    Scaffold for thermal relief spoke width analysis.

    Given only polygonal Gerbers and no netlist, we conservatively:
      - Identify "plane-like" copper polygons (large area).
      - Identify "pad-like" copper polygons (moderate area, moderate aspect).
      - For pads whose bbox is fully inside a plane bbox, we mark them as locations
        where thermal relief spokes would normally exist.

    We do not attempt to infer exact spoke widths yet, so:
      - If such pad-in-plane sites are found, we emit a WARNING with metric=None.
      - If none are found, we PASS with an info note.

    This is intentionally non-failing until we have a more robust spoke extraction.
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", "mm")

    limits = ctx.check_def.limits or {}
    recommended_min = float(limits.get("recommended_min", 0.15))
    absolute_min = float(limits.get("absolute_min", 0.10))

    raw_cfg = ctx.check_def.raw or {}

    plane_poly_min_area_mm2 = float(raw_cfg.get("plane_poly_min_area_mm2", 0.5))
    pad_min_area_mm2 = float(raw_cfg.get("pad_min_area_mm2", 0.02))
    pad_max_area_mm2 = float(raw_cfg.get("pad_max_area_mm2", 4.0))
    pad_max_aspect_ratio = float(raw_cfg.get("pad_max_aspect_ratio", 5.0))

    geom = ctx.geometry

    # Collect copper polygons
    plane_polys: List[Tuple[object, str]] = []
    pad_polys: List[Tuple[object, str]] = []

    for layer in getattr(geom, "layers", []):
        layer_type = getattr(layer, "layer_type", getattr(layer, "type", None))
        if layer_type != "copper":
            continue
        layer_name = getattr(layer, "logical_layer", None) or getattr(layer, "name", None)

        for poly in getattr(layer, "polygons", []):
            area = _poly_area_mm2(poly)
            b = poly.bounds()
            w = max(0.0, b.max_x - b.min_x)
            h = max(0.0, b.max_y - b.min_y)
            if w <= 0.0 or h <= 0.0:
                continue
            short_dim = min(w, h)
            long_dim = max(w, h)
            aspect = long_dim / short_dim if short_dim > 0.0 else 1.0

            if area >= plane_poly_min_area_mm2:
                plane_polys.append((poly, layer_name))
            elif pad_min_area_mm2 <= area <= pad_max_area_mm2 and aspect <= pad_max_aspect_ratio:
                pad_polys.append((poly, layer_name))

    if not plane_polys or not pad_polys:
        viol = Violation(
            severity="info",
            message="No plane-like or pad-like copper found; thermal relief analysis not applicable.",
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

    # Look for pad bboxes fully inside plane bboxes
    candidate_locations: List[ViolationLocation] = []

    for pad_poly, pad_layer_name in pad_polys:
        pb = pad_poly.bounds()
        for plane_poly, plane_layer_name in plane_polys:
            if plane_layer_name != pad_layer_name:
                continue
            plb = plane_poly.bounds()
            if (
                pb.min_x >= plb.min_x
                and pb.max_x <= plb.max_x
                and pb.min_y >= plb.min_y
                and pb.max_y <= plb.max_y
            ):
                cx = 0.5 * (pb.min_x + pb.max_x)
                cy = 0.5 * (pb.min_y + pb.max_y)
                candidate_locations.append(
                    ViolationLocation(
                        layer=pad_layer_name,
                        x_mm=cx,
                        y_mm=cy,
                        notes="Pad inside plane-like copper; thermal relief spokes expected here.",
                    )
                )
                break  # one plane is enough

    if not candidate_locations:
        viol = Violation(
            severity="info",
            message="No pads embedded in plane-like copper detected; no thermal relief sites found.",
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

    # We found pad-in-plane situations but cannot yet measure spokes robustly.
    # Emit a warning and a TODO-style message.
    msg = (
        f"Detected {len(candidate_locations)} pad(s) embedded in plane-like copper where thermal "
        "relief spokes would normally exist. Spoke width estimation is not yet implemented; "
        "visually confirm thermal relief geometry in these regions."
    )

    # Use first location as representative
    loc = candidate_locations[0]

    viol = Violation(
        severity="warning",
        message=msg,
        location=loc,
    )

    return CheckResult(
        check_id=ctx.check_def.id,
        name=ctx.check_def.name,
        category_id=ctx.check_def.category_id,
        severity=ctx.check_def.severity,
        status="warning",
        score=60.0,
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
