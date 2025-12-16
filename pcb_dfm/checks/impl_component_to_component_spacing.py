from __future__ import annotations

import math
from collections import defaultdict
from math import floor
from typing import Dict, List, Tuple, Optional

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


def _bbox_closest_points(b1, b2) -> Tuple[float, float, float, float, float]:
    """
    Returns (x1, y1, x2, y2, d) where (x1,y1) is closest point on b1 to b2,
    and (x2,y2) is closest point on b2 to b1, with Euclidean distance d.
    """
    # Closest x on b1 to b2
    if b2.min_x > b1.max_x:
        x1 = b1.max_x
        x2 = b2.min_x
    elif b1.min_x > b2.max_x:
        x1 = b1.min_x
        x2 = b2.max_x
    else:
        # Overlapping in X -> pick midpoint of overlap for stable marker
        ox0 = max(b1.min_x, b2.min_x)
        ox1 = min(b1.max_x, b2.max_x)
        xmid = 0.5 * (ox0 + ox1)
        x1 = xmid
        x2 = xmid

    # Closest y on b1 to b2
    if b2.min_y > b1.max_y:
        y1 = b1.max_y
        y2 = b2.min_y
    elif b1.min_y > b2.max_y:
        y1 = b1.min_y
        y2 = b2.max_y
    else:
        oy0 = max(b1.min_y, b2.min_y)
        oy1 = min(b1.max_y, b2.max_y)
        ymid = 0.5 * (oy0 + oy1)
        y1 = ymid
        y2 = ymid

    d = math.hypot(x2 - x1, y2 - y1)
    return (x1, y1, x2, y2, d)


def _bbox_size_mm(b) -> float:
    return max(0.0, max(b.max_x - b.min_x, b.max_y - b.min_y))


def _center_of_bounds(b) -> Tuple[float, float]:
    return (0.5 * (b.min_x + b.max_x), 0.5 * (b.min_y + b.max_y))


def _is_via_like(poly, via_like_max_diameter_mm: float, via_like_max_area_mm2: float, via_like_roundness: float) -> bool:
    b = poly.bounds()
    w = max(0.0, b.max_x - b.min_x)
    h = max(0.0, b.max_y - b.min_y)
    if w <= 0.0 or h <= 0.0:
        return False

    max_dim = max(w, h)
    min_dim = min(w, h)
    if max_dim > via_like_max_diameter_mm:
        return False

    # Round-ish check (vias tend to be near circular)
    if min_dim <= 0.0:
        return False
    aspect = max_dim / min_dim
    if aspect > via_like_roundness:
        return False

    area = _poly_area_mm2(poly)
    return area <= via_like_max_area_mm2


def _cell_key(x: float, y: float, cell: float) -> Tuple[int, int]:
    return (int(floor(x / cell)), int(floor(y / cell)))


@register_check("component_to_component_spacing")
def run_component_to_component_spacing(ctx: CheckContext) -> CheckResult:
    """
    Approximate component to component spacing using clustering of pad openings on the top side.

    Priority:
      1) TopSolderMask polygons (preferred - closer proxy to pad openings / footprint)
      2) TopCopper pad-like polygons (fallback)

    Notes:
      - Silkscreen is ignored in this check.
      - Via-like small round features are filtered out so vias do not create fake "components".
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", "mm")

    target_cfg = metric_cfg.get("target", {}) or {}
    limits_cfg = metric_cfg.get("limits", {}) or {}

    recommended_min = float(target_cfg.get("min", 0.5))
    absolute_min = float(limits_cfg.get("min", 0.3))

    raw_cfg = getattr(ctx.check_def, "raw", None) or {}

    # Copper pad heuristic (fallback)
    pad_min_area_mm2 = float(raw_cfg.get("pad_min_area_mm2", 0.02))
    pad_max_area_mm2 = float(raw_cfg.get("pad_max_area_mm2", 4.0))
    pad_max_aspect_ratio = float(raw_cfg.get("pad_max_aspect_ratio", 10.0))

    # Clustering
    cluster_radius_mm = float(raw_cfg.get("cluster_radius_mm", 1.5))

    # Epsilon behavior: we do NOT skip pairs, we only use epsilon to stabilize
    # "touching" detection and avoid noisy near-zero floating gaps.
    spacing_epsilon_mm = float(raw_cfg.get("spacing_epsilon_mm", 0.05))
    relative_epsilon_fraction = float(raw_cfg.get("relative_epsilon_fraction", 0.05))

    # Via-like filtering (important so vias do not inflate clusters)
    via_like_max_diameter_mm = float(raw_cfg.get("via_like_max_diameter_mm", 0.6))
    via_like_roundness = float(raw_cfg.get("via_like_roundness_aspect", 1.35))
    # Area threshold approx for circle of that diameter with some slack
    via_like_max_area_mm2 = float(
        raw_cfg.get("via_like_max_area_mm2", math.pi * (0.5 * via_like_max_diameter_mm) ** 2 * 1.25)
    )
    keep_via_if_within_mm = float(raw_cfg.get("keep_via_if_within_mm", 0.8))

    geom = ctx.geometry

    # Collect candidate polys from TopSolderMask first
    candidates: List[Tuple[object, float, float, bool]] = []  # (poly, cx, cy, is_via_like)
    used_source = "mask"

    for layer in getattr(geom, "layers", []):
        layer_type = getattr(layer, "layer_type", getattr(layer, "type", None))
        side = getattr(layer, "side", "Unknown") or "Unknown"
        if layer_type != "mask" or side.lower() != "top":
            continue
        for poly in getattr(layer, "polygons", []):
            b = poly.bounds()
            cx, cy = _center_of_bounds(b)
            is_via = _is_via_like(poly, via_like_max_diameter_mm, via_like_max_area_mm2, via_like_roundness)
            candidates.append((poly, cx, cy, is_via))

    # Fallback to TopCopper pad-like polygons if mask is missing
    if len(candidates) < 2:
        candidates = []
        used_source = "copper"
        for layer in getattr(geom, "layers", []):
            layer_type = getattr(layer, "layer_type", getattr(layer, "type", None))
            side = getattr(layer, "side", "Unknown") or "Unknown"
            if layer_type != "copper" or side.lower() != "top":
                continue

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

                cx, cy = _center_of_bounds(b)
                is_via = _is_via_like(poly, via_like_max_diameter_mm, via_like_max_area_mm2, via_like_roundness)
                candidates.append((poly, cx, cy, is_via))

    if len(candidates) < 2:
        viol = Violation(
            severity="info",
            message="Too few top side pad-like features (mask or copper) to estimate component spacing.",
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
                "kind": "distance",
                "units": units,
                "measured_value": None,
                "target": recommended_min,
                "limit_low": absolute_min,
                "limit_high": None,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    # Filter out isolated via-like features so vias do not become components.
    non_via_centers = [(cx, cy) for _, cx, cy, is_via in candidates if not is_via]

    # Grid index for neighbor queries
    cell_nv = max(keep_via_if_within_mm, 0.25)
    grid_nv: Dict[Tuple[int, int], List[Tuple[float, float]]] = defaultdict(list)
    for nx, ny in non_via_centers:
        grid_nv[_cell_key(nx, ny, cell_nv)].append((nx, ny))

    filtered: List[Tuple[object, float, float]] = []
    for poly, cx, cy, is_via in candidates:
        if not is_via:
            filtered.append((poly, cx, cy))
            continue

        keep = False
        ci, cj = _cell_key(cx, cy, cell_nv)
        # Search neighboring cells only (3x3 is enough because cell size ~= radius)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for nx, ny in grid_nv.get((ci + di, cj + dj), []):
                    if math.hypot(nx - cx, ny - cy) <= keep_via_if_within_mm:
                        keep = True
                        break
                if keep:
                    break
            if keep:
                break

        if keep:
            filtered.append((poly, cx, cy))

    pad_polys: List[Tuple[object, float, float]] = filtered

    if len(pad_polys) < 2:
        viol = Violation(
            severity="info",
            message="After filtering via-like features, too few candidates remain to estimate component spacing.",
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
                "kind": "distance",
                "units": units,
                "measured_value": None,
                "target": recommended_min,
                "limit_low": absolute_min,
                "limit_high": None,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    # Grid for clustering neighbor search
    centers: List[Tuple[float, float]] = []
    n = len(pad_polys)

    if n < 2:
        viol = Violation(
            severity="info",
            message="After filtering via-like features, too few candidates remain to estimate component spacing.",
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
                "kind": "distance",
                "units": units,
                "measured_value": None,
                "target": recommended_min,
                "limit_low": absolute_min,
                "limit_high": None,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    cell_c = max(cluster_radius_mm, 0.25)
    grid_c: Dict[Tuple[int, int], List[int]] = defaultdict(list)

    for idx, (_poly, cx, cy) in enumerate(pad_polys):
        centers.append((cx, cy))
        grid_c[_cell_key(cx, cy, cell_c)].append(idx)

    visited = [False] * n
    clusters: List[List[int]] = []

    for i in range(n):
        if visited[i]:
            continue
        stack = [i]
        visited[i] = True
        cluster: List[int] = []

        while stack:
            k = stack.pop()
            cluster.append(k)
            ckx, cky = centers[k]
            ci, cj = _cell_key(ckx, cky, cell_c)

            # Only check points in nearby cells
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for j in grid_c.get((ci + di, cj + dj), []):
                        if visited[j]:
                            continue
                        cjx, cjy = centers[j]
                        if math.hypot(cjx - ckx, cjy - cky) <= cluster_radius_mm:
                            visited[j] = True
                            stack.append(j)

        clusters.append(cluster)

    if len(clusters) < 2:
        viol = Violation(
            severity="info",
            message="Only one component cluster detected on top side; no component spacing to check.",
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
                "kind": "distance",
                "units": units,
                "measured_value": None,
                "target": recommended_min,
                "limit_low": absolute_min,
                "limit_high": None,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    # Build a bbox per cluster
    class _BBox:
        __slots__ = ("min_x", "max_x", "min_y", "max_y")

        def __init__(self, min_x, max_x, min_y, max_y):
            self.min_x = min_x
            self.max_x = max_x
            self.min_y = min_y
            self.max_y = max_y

    cluster_bboxes: List[_BBox] = []
    for cluster in clusters:
        min_x = math.inf
        max_x = -math.inf
        min_y = math.inf
        max_y = -math.inf

        for idx in cluster:
            poly, _, _ = pad_polys[idx]
            b = poly.bounds()
            min_x = min(min_x, b.min_x)
            max_x = max(max_x, b.max_x)
            min_y = min(min_y, b.min_y)
            max_y = max(max_y, b.max_y)

        if not math.isfinite(min_x) or not math.isfinite(max_x):
            continue

        cluster_bboxes.append(_BBox(min_x, max_x, min_y, max_y))

    if len(cluster_bboxes) < 2:
        viol = Violation(
            severity="info",
            message="Component clustering produced fewer than two valid clusters; skipping spacing check.",
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
                "kind": "distance",
                "units": units,
                "measured_value": None,
                "target": recommended_min,
                "limit_low": absolute_min,
                "limit_high": None,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    # Compute minimum spacing. We do not skip close pairs.
    min_spacing = math.inf
    best_midpoint: Optional[Tuple[float, float]] = None
    best_pair_is_touching = False

    m = len(cluster_bboxes)
    for i in range(m):
        bi = cluster_bboxes[i]
        size_i = _bbox_size_mm(bi)
        for j in range(i + 1, m):
            bj = cluster_bboxes[j]
            size_j = _bbox_size_mm(bj)

            # Epsilon used only to classify "near-zero" gaps as touching.
            rel_eps = relative_epsilon_fraction * max(size_i, size_j)
            effective_eps = max(spacing_epsilon_mm, rel_eps)

            x1, y1, x2, y2, d = _bbox_closest_points(bi, bj)
            is_touching = d <= effective_eps

            d_eff = 0.0 if is_touching else d
            if d_eff < min_spacing:
                min_spacing = d_eff
                best_midpoint = (0.5 * (x1 + x2), 0.5 * (y1 + y2))
                best_pair_is_touching = is_touching

    if not math.isfinite(min_spacing):
        viol = Violation(
            severity="info",
            message="No meaningful component spacing found.",
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
                "kind": "distance",
                "units": units,
                "measured_value": None,
                "target": recommended_min,
                "limit_low": absolute_min,
                "limit_high": None,
                "margin_to_limit": None,
            },
            violations=[viol],
        )

    # Classification
    if min_spacing >= recommended_min:
        status = "pass"
        severity = ctx.check_def.severity or ctx.check_def.severity_default or "warning"
        score = 100.0
    elif min_spacing < absolute_min:
        status = "warning"
        severity = "error"
        score = 0.0
    else:
        status = "warning"
        severity = "error"
        span = max(1e-6, recommended_min - absolute_min)
        frac = (min_spacing - absolute_min) / span
        score = max(0.0, min(100.0, 60.0 + 40.0 * max(0.0, frac)))

    margin_to_limit = float(min_spacing - absolute_min)

    violations: List[Violation] = []
    if status != "pass":
        extra = ""
        if best_pair_is_touching:
            extra = " (treated as touching/overlapping within epsilon)."
        msg = (
            f"Minimum component to component spacing on top side is {min_spacing:.3f} mm"
            f"{extra} (recommended >= {recommended_min:.3f} mm, absolute >= {absolute_min:.3f} mm). "
            f"Source geometry: {used_source}."
        )
        loc = None
        if best_midpoint is not None:
            loc = ViolationLocation(
                layer="TopCopper",
                x_mm=best_midpoint[0],
                y_mm=best_midpoint[1],
                notes="Closest-point midpoint between inferred component clusters (from mask openings when available).",
            )
        violations.append(
            Violation(
                severity=severity,
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
            "kind": "distance",
            "units": units,
            "measured_value": float(min_spacing),
            "target": recommended_min,
            "limit_low": absolute_min,
            "limit_high": None,
            "margin_to_limit": margin_to_limit,
        },
        violations=violations,
    )
