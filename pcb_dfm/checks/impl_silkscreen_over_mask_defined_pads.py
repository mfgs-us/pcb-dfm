from __future__ import annotations

from typing import List, Optional

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry.pad_map import get_or_build_pad_map
from ..geometry.polygon_index import PolygonIndex
from ..geometry.primitives import Bounds
from ..results import CheckResult, MetricResult, Violation, ViolationLocation


def _resolve_limit(check_def, key: str, default):
    """Resolve a threshold, preferring the pre-normalized ``check_def.limits``
    block; fall back to this check's ``metric.target``/``metric.limits`` (with
    um->mm scaling; area/count units are unscaled) when that plumbing is
    absent, so JSON thresholds are honored either way."""
    lim = getattr(check_def, "limits", None) or {}
    v = lim.get(key)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)

    metric = getattr(check_def, "metric", None) or {}
    units = str(metric.get("units", "mm")).lower()
    scale = 0.001 if units in ("um", "µm", "micron", "microns") else 1.0
    mapping = {
        "recommended_min": ("target", "min"),
        "recommended_max": ("target", "max"),
        "absolute_min": ("limits", "min"),
        "absolute_max": ("limits", "max"),
    }
    node_key, sub = mapping.get(key, (None, None))
    if node_key is not None:
        node = metric.get(node_key)
        if isinstance(node, dict):
            nv = node.get(sub)
            if isinstance(nv, (int, float)) and not isinstance(nv, bool):
                return float(nv) * scale
    return default


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


def _BBox_area(b) -> float:
    return max(0.0, (b.max_x - b.min_x)) * max(0.0, (b.max_y - b.min_y))


def _bbox_overlap_area(b1, b2) -> float:
    """Area (mm^2) of the axis-aligned intersection of two bboxes, 0 if disjoint."""
    ox = min(b1.max_x, b2.max_x) - max(b1.min_x, b2.min_x)
    oy = min(b1.max_y, b2.max_y) - max(b1.min_y, b2.min_y)
    if ox <= 0.0 or oy <= 0.0:
        return 0.0
    return ox * oy


@register_check("silkscreen_over_mask_defined_pads")
def run_silkscreen_over_mask_defined_pads(ctx: CheckContext) -> CheckResult:
    """
    Detect silkscreen ink deposited over exposed pad copper.

    NOTE on the check name: robustly classifying a pad as "solder-mask-defined"
    (SMD, mask opening smaller than the copper) vs non-mask-defined requires
    reliable mask polarity, which this engine does not yet model (see
    ``impl_solder_mask_expansion._normalize_mask_polarity``). So this check does
    the honest, tractable thing: it flags silkscreen that overlaps copper pads
    which are *exposed by a solder-mask opening* on the same side. Silk over
    mask-covered copper is harmless; silk over exposed pad copper is the real
    assembly/printing defect. If no mask layer is available we fall back to all
    pad-like copper and say so in the message.

    Metric: total silk-on-exposed-pad overlap AREA in mm^2 (bbox approximation),
    compared against the area thresholds (recommended_max / absolute_max). This
    fixes the previous unit bug where an integer overlap COUNT was compared to an
    mm^2 area limit. A large overlap area (> absolute_max) is a hard fail.
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", "mm2")

    # Area thresholds (mm^2) from the plumbed limits (with JSON fallback).
    target_max = _resolve_limit(ctx.check_def, "recommended_max", 0.0)   # mm^2
    limit_max = _resolve_limit(ctx.check_def, "absolute_max", 0.2)        # mm^2

    raw_cfg = getattr(ctx.check_def, "raw", None) or {}
    pad_min_area_mm2 = float(raw_cfg.get("pad_min_area_mm2", 0.02))
    pad_max_area_mm2 = float(raw_cfg.get("pad_max_area_mm2", 4.0))
    pad_max_aspect_ratio = float(raw_cfg.get("pad_max_aspect_ratio", 10.0))
    silk_min_area_mm2 = float(raw_cfg.get("silk_min_area_mm2", 0.01))
    mask_min_area_mm2 = float(raw_cfg.get("mask_min_area_mm2", 0.01))

    # Placement data identifies real component pads; without it "pad" is an
    # area/aspect guess that also admits pour fragments, which is why silk over
    # them could only ever be advisory.
    pad_map = get_or_build_pad_map(ctx)

    geom = ctx.geometry

    class _BBox:
        __slots__ = ("min_x", "max_x", "min_y", "max_y")

        def __init__(self, min_x, max_x, min_y, max_y):
            self.min_x = min_x
            self.max_x = max_x
            self.min_y = min_y
            self.max_y = max_y

    class _Feature:
        __slots__ = ("side", "layer", "bbox", "cx", "cy")

        def __init__(self, side, layer, poly):
            b = poly.bounds()
            self.side = side
            self.layer = layer
            self.bbox = _BBox(b.min_x, b.max_x, b.min_y, b.max_y)
            self.cx = 0.5 * (b.min_x + b.max_x)
            self.cy = 0.5 * (b.min_y + b.max_y)

    pads: List[_Feature] = []
    silks: List[_Feature] = []
    mask_openings: List[_Feature] = []

    for layer in getattr(geom, "layers", []):
        layer_type = getattr(layer, "layer_type", getattr(layer, "type", None))
        side = getattr(layer, "side", None)
        logical = getattr(layer, "logical_layer", getattr(layer, "name", None))

        if layer_type == "copper":
            for poly in getattr(layer, "polygons", []):
                if pad_map is not None and not pad_map.is_component_pad(poly):
                    continue
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
                pads.append(_Feature(side, logical, poly))

        elif layer_type in ("silkscreen", "silk"):
            for poly in getattr(layer, "polygons", []):
                area = _poly_area_mm2(poly)
                if area < silk_min_area_mm2:
                    continue
                silks.append(_Feature(side, logical, poly))

        elif layer_type == "mask":
            for poly in getattr(layer, "polygons", []):
                area = _poly_area_mm2(poly)
                if area < mask_min_area_mm2:
                    continue
                mask_openings.append(_Feature(side, logical, poly))

    if not pads or not silks:
        viol = Violation(
            severity="info",
            message="No silkscreen and pad-like copper both present; nothing to check.",
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
                kind="area",
                units=units,
                measured_value=0.0,
                target=target_max,
                limit_high=limit_max,
                margin_to_limit=limit_max,
            ),
            violations=[viol],
        ).finalize()

    have_mask = bool(mask_openings)

    # Spatially index mask openings so pad exposure is a local query, not a scan
    # of every opening per pad (both sets run to thousands on real boards).
    mask_index = PolygonIndex.from_bounds(
        [(i, Bounds(m.bbox.min_x, m.bbox.min_y, m.bbox.max_x, m.bbox.max_y))
         for i, m in enumerate(mask_openings)]
    ) if mask_openings else None

    def _pad_is_exposed(pad: _Feature) -> bool:
        """A pad is treated as exposed if a same-side mask opening overlaps it.
        Without a mask layer, treat all pads as exposed (documented fallback)."""
        if not have_mask or mask_index is None:
            return True
        pb = Bounds(pad.bbox.min_x, pad.bbox.min_y, pad.bbox.max_x, pad.bbox.max_y)
        for mi in mask_index.query_bbox(pb):
            m = mask_openings[mi]
            if m.side and pad.side and str(m.side).lower() != str(pad.side).lower():
                continue
            if _bboxes_overlap(m.bbox, pad.bbox):
                return True
        return False

    exposed_pads = [p for p in pads if _pad_is_exposed(p)]

    # Accumulate silk overlap PER PAD, capped at each pad's own area.
    #
    # Silk is drawn as thousands of tessellated stroke segments, each with its
    # own (inflated) bbox, so summing every silk x pad bbox overlap double-counts
    # wildly -- a diagonal stroke's bbox is far larger than its ink, and many
    # strokes clip the same pad's corners. On real boards that produced hundreds
    # of tiny overlaps summing to tens of mm^2 (pcbtools_full: 1668 pairs,
    # 51 mm^2) against a 0.2 mm^2 fail threshold, so every real board failed
    # (#19). Silk physically cannot cover more of a pad than the pad's area, so
    # capping each pad's accumulated overlap at its own area bounds the
    # double-count; a pad only counts once it is meaningfully covered.
    per_pad_overlap: dict[int, float] = {}
    per_pad_loc: dict[int, ViolationLocation] = {}
    pad_area = [(_BBox_area(p.bbox)) for p in exposed_pads]

    # Index the pads and query each silk stroke's bbox against it. The raw
    # silk x pad double loop is O(n*m) and both run to thousands on a real
    # board (pcbtools_full: ~7500 silk strokes x ~1400 pads), which took ~11 s
    # for this one check; the index makes it local.
    pad_index = PolygonIndex.from_bounds(
        [(idx, Bounds(p.bbox.min_x, p.bbox.min_y, p.bbox.max_x, p.bbox.max_y))
         for idx, p in enumerate(exposed_pads)]
    ) if exposed_pads else None

    if pad_index is not None:
        for sf in silks:
            sb = Bounds(sf.bbox.min_x, sf.bbox.min_y, sf.bbox.max_x, sf.bbox.max_y)
            for idx in pad_index.query_bbox(sb):
                p = exposed_pads[idx]
                if sf.side and p.side and str(sf.side).lower() != str(p.side).lower():
                    continue
                area = _bbox_overlap_area(sf.bbox, p.bbox)
                if area <= 0.0:
                    continue
                prev = per_pad_overlap.get(idx, 0.0)
                per_pad_overlap[idx] = min(pad_area[idx], prev + area)
                if idx not in per_pad_loc:
                    per_pad_loc[idx] = ViolationLocation(
                        layer=sf.layer, x_mm=p.cx, y_mm=p.cy,
                        notes="Exposed pad with silkscreen ink over it (bbox approximation).",
                    )

    # A pad is "silk-covered" only if silk covers a meaningful fraction of it,
    # which discards the thin-stroke corner clips that dominated the old sum.
    min_cover_frac = float(raw_cfg.get("min_pad_cover_fraction", 0.15))
    covered = {
        idx: a for idx, a in per_pad_overlap.items()
        if pad_area[idx] > 0.0 and a >= min_cover_frac * pad_area[idx]
    }
    total_overlap_area = sum(covered.values())
    overlap_pairs = len(covered)
    worst_area = 0.0
    worst_loc: Optional[ViolationLocation] = None
    for idx, a in covered.items():
        if a > worst_area:
            worst_area = a
            worst_loc = per_pad_loc.get(idx)

    measured = float(total_overlap_area)

    if measured <= target_max:
        status = "pass"
        severity = "info"
        score = 100.0
    elif measured <= limit_max:
        status = "warning"
        severity = "warning"
        span = max(1e-9, limit_max - target_max)
        frac = max(0.0, min(1.0, (measured - target_max) / span))
        score = max(0.0, min(100.0, 100.0 - 40.0 * frac))
    elif pad_map is not None:
        # Placement data confirms these are real component pads, so silk printed
        # on them is a genuine solderability defect and may fail again.
        status = "fail"
        severity = "error"
        score = 0.0
    else:
        # ADVISORY without placement data -- a bbox overlap estimate with no
        # boolean geometry cannot cleanly separate ink genuinely on a pad from a
        # stroke passing near it or silk on an adjacent pour. Real silk-on-pad
        # still warns; it just does not fail a board on the approximation (#19).
        status = "warning"
        severity = "warning"
        score = 40.0

    margin_to_limit = float(limit_max - measured)

    fallback_note = "" if have_mask else " (no mask layer available; evaluated all pad-like copper)"

    violations: List[Violation] = []
    if status == "pass":
        violations.append(
            Violation(
                severity="info",
                message=f"No significant silkscreen-over-pad overlap detected{fallback_note}.",
                location=None,
            )
        )
    else:
        msg = (
            f"Silkscreen overlaps exposed pad copper over ~{measured:.3f} mm^2 "
            f"across {overlap_pairs} region(s){fallback_note} "
            f"(recommended <= {target_max:.3f} mm^2, absolute <= {limit_max:.3f} mm^2). "
            f"Silk ink on pads should be clipped."
        )
        violations.append(
            Violation(
                severity=severity,
                message=msg,
                location=worst_loc,
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
            kind="area",
            units=units,
            measured_value=measured,
            target=target_max,
            limit_high=limit_max,
            margin_to_limit=margin_to_limit,
        ),
        violations=violations,
    ).finalize()
