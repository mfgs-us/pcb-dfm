from __future__ import annotations

import math
from typing import List, Tuple, Optional

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


@register_check("component_to_component_spacing")
def run_component_to_component_spacing(ctx: CheckContext) -> CheckResult:
    """
    Approximate component to component spacing using pad clustering on the top side.

    Metric is minimum body to body distance (mm) between any two inferred components,
    matching the JSON config:
      - metric.kind: "distance"
      - metric.units: "mm"
      - target.min: 0.5
      - limits.min: 0.3
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", "mm")

    target_cfg = metric_cfg.get("target", {}) or {}
    limits_cfg = metric_cfg.get("limits", {}) or {}

    recommended_min = float(target_cfg.get("min", 0.5))
    absolute_min = float(limits_cfg.get("min", 0.3))

    raw_cfg = getattr(ctx.check_def, "raw", None) or {}
    pad_min_area_mm2 = float(raw_cfg.get("pad_min_area_mm2", 0.02))
    pad_max_area_mm2 = float(raw_cfg.get("pad_max_area_mm2", 4.0))
    pad_max_aspect_ratio = float(raw_cfg.get("pad_max_aspect_ratio", 10.0))
    cluster_radius_mm = float(raw_cfg.get("cluster_radius_mm", 1.5))

    # Epsilon filter to ignore trivial or artifact gaps
    spacing_epsilon_mm = float(raw_cfg.get("spacing_epsilon_mm", 0.05))
    relative_epsilon_fraction = float(raw_cfg.get("relative_epsilon_fraction", 0.05))

    geom = ctx.geometry

    # Collect top side pad like polys
    pad_polys: List[Tuple[object, float, float]] = []  # (poly, cx, cy)

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

            cx = 0.5 * (b.min_x + b.max_x)
            cy = 0.5 * (b.min_y + b.max_y)
            pad_polys.append((poly, cx, cy))

    if len(pad_polys) < 2:
        viol = Violation(
            severity="info",
            message="Too few top side pad like features to estimate component spacing.",
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

    # Cluster pad centers into components
    n = len(pad_polys)
    centers = [(cx, cy) for _, cx, cy in pad_polys]
    visited = [False] * n
    clusters: List[List[int]] = []

    for i in range(n):
        if visited[i]:
            continue
        stack = [i]
        visited[i] = True
        cluster = []
        while stack:
            k = stack.pop()
            cluster.append(k)
            ckx, cky = centers[k]
            for j in range(n):
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
    cluster_centers: List[Tuple[float, float]] = []

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
        cluster_centers.append((0.5 * (min_x + max_x), 0.5 * (min_y + max_y)))

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

    # Compute minimum spacing, with epsilon filter
    min_spacing = math.inf
    worst_pair_center: Optional[Tuple[float, float]] = None

    m = len(cluster_bboxes)
    for i in range(m):
        bi = cluster_bboxes[i]
        size_i = max(bi.max_x - bi.min_x, bi.max_y - bi.min_y)
        for j in range(i + 1, m):
            bj = cluster_bboxes[j]
            size_j = max(bj.max_x - bj.min_x, bj.max_y - bj.min_y)
            d = _bbox_distance_mm(bi, bj)

            # Ignore "clearances" that are tiny compared to component sizes
            rel_eps = relative_epsilon_fraction * max(size_i, size_j)
            effective_eps = max(spacing_epsilon_mm, rel_eps)

            if d <= effective_eps:
                # Treat as touching or same component for DFM spacing purposes
                continue

            if d < min_spacing:
                min_spacing = d
                cx = 0.5 * (cluster_centers[i][0] + cluster_centers[j][0])
                cy = 0.5 * (cluster_centers[i][1] + cluster_centers[j][1])
                worst_pair_center = (cx, cy)

    if not math.isfinite(min_spacing):
        # Everything either overlaps or is within epsilon, so we have no
        # meaningful component to component spacing to report.
        viol = Violation(
            severity="info",
            message="No meaningful component to component spacing found (all clusters overlap or are closer than spacing epsilon).",
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
        status = "fail"
        severity = "error"
        score = 0.0
    else:
        status = "warning"
        severity = "warning"
        span = max(1e-6, recommended_min - absolute_min)
        frac = (min_spacing - absolute_min) / span
        score = max(0.0, min(100.0, 60.0 + 40.0 * max(0.0, frac)))

    margin_to_limit = float(min_spacing - absolute_min)

    violations: List[Violation] = []
    if status != "pass":
        msg = (
            f"Minimum component to component spacing on top side is {min_spacing:.3f} mm "
            f"(recommended >= {recommended_min:.3f} mm, absolute >= {absolute_min:.3f} mm)."
        )
        loc = None
        if worst_pair_center is not None:
            loc = ViolationLocation(
                layer="TopCopper",
                x_mm=worst_pair_center[0],
                y_mm=worst_pair_center[1],
                notes="Approximate center between closest inferred component clusters.",
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
