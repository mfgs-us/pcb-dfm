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


def copper_in_polygon(geom, region: List[Tuple[float, float]],
                      layers: List[str] | None = None) -> List[Tuple[str, float, float]]:
    """Copper polygons whose centroid falls inside ``region`` (a closed (x,y)
    outline). Restricts to the named copper layers when given. Returns
    (layer_name, cx, cy) for each hit. Used by the antenna keep-out check."""
    from ..geometry.primitives import Point2D

    region_pts = [Point2D(x=x, y=y) for (x, y) in region]
    want = {l.lower() for l in layers} if layers else None
    hits: List[Tuple[str, float, float]] = []
    for layer in queries.get_copper_layers(geom):
        lname = str(getattr(layer, "logical_layer", "") or "")
        if want is not None and lname.lower() not in want:
            continue
        for poly in layer.polygons:
            b = poly.bounds()
            cx, cy = 0.5 * (b.min_x + b.max_x), 0.5 * (b.min_y + b.max_y)
            if _point_in_polygon(cx, cy, region_pts):
                hits.append((lname, cx, cy))
    return hits
