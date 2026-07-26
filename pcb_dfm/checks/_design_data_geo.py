"""Geometry-touching helpers shared by the Tier-2 design-data checks.

These bridge ``DesignData`` (net/pad locations) to the artwork (mask openings,
copper) -- the piece that could not live in ``ingest/design_intel.py`` without a
circular dependency on the check/geometry layer.
"""

from __future__ import annotations

from typing import Callable, List, Tuple

from ..geometry import queries
from ..geometry.polygon_index import PolygonIndex
from ..geometry.primitives import Bounds
from .impl_min_annular_ring import _point_in_polygon


def mask_opening_lookup(geom) -> Callable[[float, float], bool]:
    """Return ``exposed(x, y)`` -> True when the point falls inside a solder-mask
    opening (i.e. exposed copper a probe could touch). Returns a predicate that
    is always False when the board has no mask layer (caller should treat that as
    "cannot determine" / not_applicable rather than "nothing exposed"). E6.
    """
    polys = [p for layer in queries.get_mask_layers(geom) for p in layer.polygons]
    if not polys:
        return lambda x, y: False
    index = PolygonIndex.from_bounds(
        [(i, p.bounds()) for i, p in enumerate(polys)]
    )

    def exposed(x: float, y: float) -> bool:
        for i in index.query_bbox(Bounds(x, y, x, y)):
            if _point_in_polygon(x, y, polys[i].vertices):
                return True
        return False

    return exposed


def has_mask_layer(geom) -> bool:
    return any(layer.polygons for layer in queries.get_mask_layers(geom))


def copper_lookup(geom) -> Callable[[float, float], bool]:
    """Return ``on_copper(x, y)`` -> True when the point falls inside a copper
    polygon. Used to tell a genuinely tented access point (on copper, no mask
    opening) from a mis-registered one (floating in empty space): the latter
    means the netlist is not aligned to the artwork and testability cannot be
    judged. Always False when the board has no copper."""
    polys = [p for layer in queries.get_copper_layers(geom) for p in layer.polygons]
    if not polys:
        return lambda x, y: False
    index = PolygonIndex.from_bounds([(i, p.bounds()) for i, p in enumerate(polys)])

    def on_copper(x: float, y: float) -> bool:
        for i in index.query_bbox(Bounds(x, y, x, y)):
            if _point_in_polygon(x, y, polys[i].vertices):
                return True
        return False

    return on_copper


def copper_in_polygon(geom, region: List[Tuple[float, float]],
                      layers: List[str] | None = None) -> List[Tuple[str, float, float]]:
    """Copper polygons that OVERLAP ``region`` (a closed (x,y) outline).

    Restricts to the named copper layers when given. Returns (layer_name, x, y)
    for each hit, the point being inside the region. Used by the antenna keep-out
    check.

    Overlap -- not just centroid-in-region -- because a large ground pour can
    cover the whole keep-out while its own centroid sits far outside it. Detected
    three ways (bbox-prefiltered): the copper centroid is in the region, a copper
    vertex is in the region, or a region vertex is inside the copper (the pour
    case). This stays a conservative test; exact polygon intersection is not
    needed for an advisory keep-out.
    """
    from ..geometry.primitives import Point2D

    region_pts = [Point2D(x=x, y=y) for (x, y) in region]
    rx0 = min(x for (x, _) in region)
    ry0 = min(y for (_, y) in region)
    rx1 = max(x for (x, _) in region)
    ry1 = max(y for (_, y) in region)
    want = {l.lower() for l in layers} if layers else None
    hits: List[Tuple[str, float, float]] = []
    for layer in queries.get_copper_layers(geom):
        lname = str(getattr(layer, "logical_layer", "") or "")
        if want is not None and lname.lower() not in want:
            continue
        for poly in layer.polygons:
            b = poly.bounds()
            if b.max_x < rx0 or b.min_x > rx1 or b.max_y < ry0 or b.min_y > ry1:
                continue  # bboxes disjoint -> no overlap
            cx, cy = 0.5 * (b.min_x + b.max_x), 0.5 * (b.min_y + b.max_y)
            verts = poly.vertices
            if _point_in_polygon(cx, cy, region_pts):
                hits.append((lname, cx, cy))
                continue
            hit = next((v for v in verts
                        if _point_in_polygon(v.x, v.y, region_pts)), None)
            if hit is not None:
                hits.append((lname, hit.x, hit.y))
                continue
            # A region vertex inside the copper -> the copper covers the region
            # (large pour whose centroid/verts are all outside the small region).
            rv = next((p for p in region_pts
                       if _point_in_polygon(p.x, p.y, verts)), None)
            if rv is not None:
                hits.append((lname, rv.x, rv.y))
    return hits
