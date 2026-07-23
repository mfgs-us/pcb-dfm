from __future__ import annotations

import math
from collections import defaultdict
from math import floor
from typing import Dict, List, Optional, Tuple

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


def _components_from_design_data(ctx: CheckContext, side_key: str):
    """Absolute pad positions grouped by component ref, for one board side.

    Returns None when the board carries no usable placement data, so the caller
    falls back to the geometric clustering heuristic.
    """
    dd = getattr(ctx, "design_data", None)
    comps = getattr(dd, "components", None) if dd is not None else None
    if not comps:
        return None

    out: List[Tuple[str, float, float]] = []
    for c in comps:
        if not getattr(c, "placed", True) or not getattr(c, "pads", None):
            continue
        c_side = (getattr(c, "side", None) or "").lower()
        # A component with no recorded side is assumed to be on top, matching
        # the rest of this check's top-side-only scope.
        if c_side and c_side != side_key:
            continue
        for pad in c.pads:
            out.append((c.ref, pad.x_mm, pad.y_mm))

    return out or None


def _label_by_component(pad_polys, design_pads, max_match_mm: float):
    """Map each copper/mask feature to a component ref via its nearest design pad.

    Geometry has to come from the artwork -- design-data pads are points and
    carry no size -- but *identity* comes from the placement file, which is the
    only source that actually knows which pads belong to the same part. Features
    with no design pad within ``max_match_mm`` (vias, fiducials, test points)
    return None and are dropped by the caller: they are not component pads, so
    they cannot participate in component-to-component spacing.
    """
    labels: List[Optional[str]] = []
    for _poly, cx, cy in pad_polys:
        best_ref: Optional[str] = None
        best_d = max_match_mm
        for ref, px, py in design_pads:
            d = math.hypot(px - cx, py - cy)
            if d <= best_d:
                best_d = d
                best_ref = ref
        labels.append(best_ref)
    return labels


def _is_pad_plausible(poly, min_area_mm2: float, max_area_mm2: float, max_aspect: float) -> bool:
    """Could this polygon be a single component pad?

    Screens out both the too-small (degenerate pour-boundary artifacts) and the
    too-large / too-elongated (planes, keep-out regions, long mask reliefs). Used
    for the solder-mask and the copper candidate sources alike, so one stray
    board-scale region cannot become a "component".
    """
    area = _poly_area_mm2(poly)
    if area < min_area_mm2 or area > max_area_mm2:
        return False
    b = poly.bounds()
    w = max(0.0, b.max_x - b.min_x)
    h = max(0.0, b.max_y - b.min_y)
    if w <= 0.0 or h <= 0.0:
        return False
    short_dim, long_dim = min(w, h), max(w, h)
    return (long_dim / short_dim if short_dim > 0.0 else 1.0) <= max_aspect


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

    IMPORTANT (measurement honesty): without component placement/footprint data
    this check has no true component bodies. It clusters pad openings into
    inferred "components" and measures the GAP BETWEEN PAD CLUSTERS. That gap is
    only a proxy for real body-to-body clearance and is generally an OVER-ESTIMATE
    of it (component bodies usually extend beyond their pads, so the true body gap
    is smaller than the pad-cluster gap). Treat a pass here as necessary but not
    sufficient; a failure (touching/overlapping clusters) is a hard signal.
    """
    metric_cfg = ctx.check_def.metric or {}
    units = metric_cfg.get("units", "mm")

    target_cfg = metric_cfg.get("target", {}) or {}
    limits_cfg = metric_cfg.get("limits", {}) or {}

    recommended_min = float(target_cfg.get("min", 0.5))
    absolute_min = float(limits_cfg.get("min", 0.3))

    raw_cfg = getattr(ctx.check_def, "raw", None) or {}

    # Pad plausibility. Applied to BOTH candidate sources: a solder-mask layer
    # carries plenty of openings that are not component pads (large keep-out or
    # cutout regions), and letting one through makes it a "component" whose
    # bounding box overlaps half the board -- reported as 0.00 mm spacing (#14).
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
            if not _is_pad_plausible(poly, pad_min_area_mm2, pad_max_area_mm2, pad_max_aspect_ratio):
                continue
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
                if not _is_pad_plausible(poly, pad_min_area_mm2, pad_max_area_mm2, pad_max_aspect_ratio):
                    continue

                b = poly.bounds()
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
            severity="info",
            status="not_applicable",
            score=None,
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
            severity="info",
            status="not_applicable",
            score=None,
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
            severity="info",
            status="not_applicable",
            score=None,
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

    # Prefer real component identity over geometric clustering when the board
    # supplies placement data (#14). Clustering pads by proximity cannot tell a
    # 2.54 mm-pitch connector from two adjacent parts -- any radius that keeps
    # one connector together merges genuinely separate components, and any
    # radius that separates them splits the connector against itself. The
    # placement file simply knows.
    clusters: List[List[int]] = []
    design_pads = _components_from_design_data(ctx, "top")
    if design_pads:
        labels = _label_by_component(pad_polys, design_pads, cluster_radius_mm * 2.0)
        by_ref: Dict[str, List[int]] = defaultdict(list)
        for idx, ref in enumerate(labels):
            if ref is not None:
                by_ref[ref].append(idx)
        if len(by_ref) >= 2:
            clusters = list(by_ref.values())
            used_source = f"{used_source}+placement"

    if not clusters:
        visited = [False] * n

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
                            near = math.hypot(cjx - ckx, cjy - cky) <= cluster_radius_mm
                            # Two pads whose shapes physically overlap cannot belong
                            # to different components -- that is one footprint, not a
                            # collision. Without this, a coarse cluster_radius_mm
                            # splits a single connector (2.54 mm pitch vs a 1.5 mm
                            # radius) into "components" whose own pads overlap, and
                            # the check reports 0.00 mm spacing against itself (#14).
                            if not near:
                                *_, gap = _bbox_closest_points(
                                    pad_polys[k][0].bounds(), pad_polys[j][0].bounds())
                                near = gap <= 0.0
                            if near:
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
            severity="info",
            status="not_applicable",
            score=None,
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
            severity="info",
            status="not_applicable",
            score=None,
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

    # Compute minimum spacing between *pads of different clusters*, not between
    # the clusters' bounding boxes (#14).
    #
    # Two problems with the bbox-pair approach it replaces:
    #   1. A cluster bbox is a poor stand-in for a component. Interleaved or
    #      L-shaped placements have overlapping bboxes while the components
    #      themselves are comfortably apart, and an overlap reports 0.0 mm.
    #   2. The "touching" epsilon scaled with cluster size
    #      (relative_epsilon_fraction * bbox extent) and then SNAPPED the gap to
    #      exactly 0.0. Single-link clustering on a dense board yields very large
    #      clusters, so the epsilon grew to a millimetre or more and swallowed
    #      real, healthy gaps -- reporting a hard collision where none existed.
    #
    # Pads in different clusters are more than cluster_radius_mm apart by
    # construction, so a bounded neighbourhood search finds the true minimum;
    # we only fall back to the coarse cluster-bbox distance when no cross-cluster
    # pair is near enough to matter.
    cluster_of: Dict[int, int] = {}
    for cid, cluster in enumerate(clusters):
        for idx in cluster:
            cluster_of[idx] = cid

    cell_p = max(cluster_radius_mm, 1.0)
    grid_p: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for idx, (_poly, cx, cy) in enumerate(pad_polys):
        grid_p[_cell_key(cx, cy, cell_p)].append(idx)

    min_spacing = math.inf
    best_midpoint: Optional[Tuple[float, float]] = None

    for i, (poly_i, cx, cy) in enumerate(pad_polys):
        ci_key, cj_key = _cell_key(cx, cy, cell_p)
        bi = poly_i.bounds()
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for j in grid_p.get((ci_key + di, cj_key + dj), []):
                    if j <= i or cluster_of.get(i) == cluster_of.get(j):
                        continue
                    bj = pad_polys[j][0].bounds()
                    x1, y1, x2, y2, d = _bbox_closest_points(bi, bj)
                    if d < min_spacing:
                        min_spacing = d
                        best_midpoint = (0.5 * (x1 + x2), 0.5 * (y1 + y2))

    if not math.isfinite(min_spacing):
        # No cross-cluster pad pair within the search neighbourhood: the
        # components are far apart, so the coarse cluster-bbox distance is a
        # perfectly good answer and precision does not matter here.
        m = len(cluster_bboxes)
        for i in range(m):
            for j in range(i + 1, m):
                x1, y1, x2, y2, d = _bbox_closest_points(cluster_bboxes[i], cluster_bboxes[j])
                if d < min_spacing:
                    min_spacing = d
                    best_midpoint = (0.5 * (x1 + x2), 0.5 * (y1 + y2))

    best_pair_is_touching = min_spacing <= spacing_epsilon_mm

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
            severity="info",
            status="not_applicable",
            score=None,
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
        # Below the absolute minimum (including spacing 0 for touching/overlapping
        # clusters) is a hard collision that must FAIL, not merely warn.
        status = "fail"
        severity = "error"
        score = 0.0
    else:
        # Between absolute and recommended: a real concern but not a collision.
        # Warning-status violations must carry warning severity (an error-severity
        # violation here would inflate the summary's error count).
        status = "warning"
        severity = "warning"
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
    ).finalize()
