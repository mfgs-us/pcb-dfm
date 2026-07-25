from __future__ import annotations

import math
from typing import List, Optional

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.polygon_index import PolygonIndex
from ..geometry.primitives import Bounds
from ..results import CheckResult, Violation, ViolationLocation
from .impl_solder_mask_expansion import _min_distance_between_polygons


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


@register_check("solder_mask_web")
def run_solder_mask_web(ctx: CheckContext) -> CheckResult:
    """
    Minimum mask web width between adjacent mask openings.

    Openings are paired by true bounding box within the recommended web, so the
    web is measured regardless of pad size (the previous centroid-block pairing
    silently missed webs between any pads more than ~0.75 mm pitch). A
    sub-minimum web is reported as an ADVISORY warning, not a hard fail: whether
    a thin web is a bridging defect or acceptable fine-pitch/same-net relief
    needs adjacent-net data the Gerbers lack (see the status block below).

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

    # Spatial index over each opening's TRUE bounding box (not its centroid).
    # The previous index stored each opening as a degenerate point at its
    # centroid and paired opening i only with openings whose centroid fell in a
    # fixed ~0.75 mm cell block around it. That silently missed thin webs between
    # openings larger than the block: two ordinary 1 mm pads have centroids
    # ~1 mm apart, so no matter how narrow the mask web between their *edges*,
    # the pair was never formed and the web never measured -- a solder-bridging
    # escape on exactly the boards it matters for.
    #
    # Indexing by bounding box and querying neighbours within `recommended_min`
    # makes the candidate set depend on edge proximity, not opening size. This
    # never misses a sub-threshold web: the true edge-to-edge distance is always
    # >= the bbox distance, so any pair whose web is below the threshold has
    # bboxes within the threshold and is therefore returned by the query.
    opening_bounds = [
        Bounds(o.min_x, o.min_y, o.max_x, o.max_y) for o in openings
    ]
    index = PolygonIndex.from_bounds(list(enumerate(opening_bounds)))

    min_spacing = math.inf
    min_loc: Optional[ViolationLocation] = None

    for i, oi in enumerate(openings):
        for j in index.nearby(opening_bounds[i], recommended_min):
            if j <= i:
                continue
            oj = openings[j]

            if oi.side and oj.side and str(oi.side).lower() != str(oj.side).lower():
                continue

            # bbox distance is a valid lower bound; skip the exact computation
            # when it can't beat the current best.
            if _bbox_distance_mm(opening_bounds[i], opening_bounds[j]) >= min_spacing:
                continue
            # TRUE edge-to-edge web width between the two opening polygons
            # (bbox gap under-measured rotated/diagonal openings).
            d = _min_distance_between_polygons(oi.poly, oj.poly)
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
    else:
        # ADVISORY, not a hard fail. A sub-minimum mask web is a real geometric
        # condition, but whether it is a bridging DEFECT depends on whether the
        # two adjacent openings sit on DIFFERENT nets -- data bare Gerbers do not
        # carry. Fine-pitch parts routinely have thin or ganged webs that are
        # perfectly acceptable, so hard-failing on web width alone is a false-
        # positive generator on real boards. Mirroring via_to_copper_clearance,
        # we surface a thin web as a warning for review; a net-aware upgrade can
        # promote confirmed different-net adjacency to a fail (the capability
        # this advisory downgrade holds in trust, not a permanent concession).
        status = "warning"
        severity = "warning"
        # Linear from 0 (touching) to 100 (at the recommended web), so a
        # near-zero web scores near zero even though it is "only" a warning.
        score = max(0.0, min(100.0, 100.0 * measured / recommended_min))

    margin_to_limit = float(measured - absolute_min)

    msg = (
        f"Minimum solder mask web width is {measured:.3f} mm "
        f"(recommended >= {recommended_min:.3f} mm, absolute >= {absolute_min:.3f} mm). "
        f"Verify adjacent openings are same-net or increase the web."
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
