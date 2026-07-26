"""Design advisory: every component should carry a nearby reference designator.

Assembly and rework identify parts by their silkscreen refdes. We infer
components by clustering nearby copper pads, then check each multi-pad cluster
for a silkscreen feature within a label search radius. Heuristic (no schematic),
so it warns, never fails, and only considers clusters of two-plus pads -- a lone
pad (test point, via, fiducial) is not a "component".
"""

from __future__ import annotations

from collections import defaultdict
from math import floor
from typing import Dict, List, Tuple

from ..engine.check_runner import register_check
from ..engine.context import CheckContext
from ..geometry import queries
from ..geometry.polygon_index import PolygonIndex
from ..geometry.primitives import Bounds
from ..results import CheckResult, ViolationLocation
from ._design_advisory import advisory, count_metric, is_pad_like, na

_CLUSTER_GAP_MM = 1.5     # pads within this are the same component
_LABEL_RADIUS_MM = 4.0    # silk within this of the cluster is its refdes


def _cluster(points: List[Tuple[float, float]], gap: float) -> List[List[int]]:
    """Grid + union-find proximity clustering."""
    parent = list(range(len(points)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    cell = max(gap, 0.5)
    grid: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for i, (x, y) in enumerate(points):
        grid[(int(floor(x / cell)), int(floor(y / cell)))].append(i)
    for i, (x, y) in enumerate(points):
        ci, cj = int(floor(x / cell)), int(floor(y / cell))
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for j in grid.get((ci + di, cj + dj), []):
                    if j <= i:
                        continue
                    dx, dy = points[i][0] - points[j][0], points[i][1] - points[j][1]
                    if dx * dx + dy * dy <= gap * gap:
                        union(i, j)
    groups: Dict[int, List[int]] = defaultdict(list)
    for i in range(len(points)):
        groups[find(i)].append(i)
    return list(groups.values())


@register_check("reference_designator_coverage")
def run_reference_designator_coverage(ctx: CheckContext) -> CheckResult:
    geom = ctx.geometry
    silk_layers = queries.get_silkscreen_layers(geom)
    if not silk_layers:
        return na(ctx, "No silkscreen layer to check reference-designator coverage.")

    pad_centers: List[Tuple[float, float]] = []
    for layer in queries.get_copper_layers(geom):
        for poly in layer.polygons:
            if is_pad_like(poly):
                b = poly.bounds()
                pad_centers.append((0.5 * (b.min_x + b.max_x), 0.5 * (b.min_y + b.max_y)))
    if len(pad_centers) < 2:
        return na(ctx, "Too few component pads to infer components.")

    # Silk feature centroids, indexed for a radius query.
    silk_pts: List[Tuple[float, float]] = []
    for layer in silk_layers:
        for poly in getattr(layer, "polygons", []):
            b = poly.bounds()
            silk_pts.append((0.5 * (b.min_x + b.max_x), 0.5 * (b.min_y + b.max_y)))
    silk_index = PolygonIndex.from_bounds(
        [(i, Bounds(x, y, x, y)) for i, (x, y) in enumerate(silk_pts)]
    ) if silk_pts else None

    clusters = [c for c in _cluster(pad_centers, _CLUSTER_GAP_MM) if len(c) >= 2]
    if not clusters:
        return na(ctx, "No multi-pad component clusters inferred.")

    uncovered: List[Tuple[float, float]] = []
    for c in clusters:
        cx = sum(pad_centers[i][0] for i in c) / len(c)
        cy = sum(pad_centers[i][1] for i in c) / len(c)
        has_label = False
        if silk_index is not None:
            q = Bounds(cx - _LABEL_RADIUS_MM, cy - _LABEL_RADIUS_MM,
                       cx + _LABEL_RADIUS_MM, cy + _LABEL_RADIUS_MM)
            for sid in silk_index.query_bbox(q):
                sx, sy = silk_pts[sid]
                if (sx - cx) ** 2 + (sy - cy) ** 2 <= _LABEL_RADIUS_MM ** 2:
                    has_label = True
                    break
        if not has_label:
            uncovered.append((cx, cy))

    count = len(uncovered)
    total = len(clusters)
    if count == 0:
        return advisory(ctx, False, count_metric(0),
                        f"All {total} inferred components have a nearby silkscreen label.")
    x, y = uncovered[0]
    return advisory(
        ctx, True, count_metric(count),
        f"{count} of {total} inferred component(s) have no silkscreen reference "
        f"designator within {_LABEL_RADIUS_MM:.0f} mm. Add refdes labels for "
        f"assembly / rework identification.",
        ViolationLocation(layer="Silkscreen", x_mm=x, y_mm=y,
                          notes="Component with no nearby reference designator."),
    )
