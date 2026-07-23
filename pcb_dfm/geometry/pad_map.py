"""Identify which copper polygons are real component pads, from placement data.

Several checks are about component *pads* specifically -- mask expansion around
a pad, silkscreen printed on a pad, a via dropped into a pad. From artwork alone
a "pad" can only be guessed at by area and aspect ratio, and that guess is what
made those checks unreliable: a trace stub, a pour finger or a via's own landing
ring all pass a shape test, so they were measured as pads and produced findings
on copper that is not a pad at all.

Placement data settles it. A footprint states where each pad of each component
sits, so any copper polygon containing one of those points IS that component's
pad, and everything else is not. That is the same containment argument the
netlist path uses for nets, and it is equally direct.

Deliberately NOT propagated through connected copper (unlike nets): a pad is one
specific polygon. The trace leaving it is on the same net but is not part of the
pad, and treating it as one would recreate the exact confusion this removes.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple, cast

from ..ingest.design_model import DesignData
from .layer_model import BoardGeometry
from .polygon_index import PolygonIndex
from .primitives import Bounds, Polygon


def _point_in_polygon(x: float, y: float, poly: Polygon) -> bool:
    v = poly.vertices
    n = len(v)
    if n < 3:
        return False
    inside = False
    for i in range(n):
        j = (i + 1) % n
        xi, yi = v[i].x, v[i].y
        xj, yj = v[j].x, v[j].y
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
    return inside


class PadMap:
    """Copper polygons that are real component pads, per placement data."""

    def __init__(
        self,
        pad_polys: Dict[int, Tuple[str, Optional[str]]],
        by_component: Dict[str, List[Polygon]],
    ) -> None:
        # polygon id -> (component ref, pad name)
        self._pad_polys = pad_polys
        self._by_component = by_component

    def is_component_pad(self, poly: Polygon) -> bool:
        return id(poly) in self._pad_polys

    def component_of(self, poly: Polygon) -> Optional[str]:
        entry = self._pad_polys.get(id(poly))
        return entry[0] if entry else None

    def pad_name_of(self, poly: Polygon) -> Optional[str]:
        entry = self._pad_polys.get(id(poly))
        return entry[1] if entry else None

    def pad_polygon_count(self) -> int:
        return len(self._pad_polys)

    def components(self) -> List[str]:
        return sorted(self._by_component)


def build_pad_map(
    geometry: BoardGeometry,
    design_data: Optional[DesignData],
) -> Optional[PadMap]:
    """Map copper polygons to the component pads they represent.

    Returns None when there is no placement data to work from, so callers can
    fall back to their artwork-only behaviour rather than silently treating a
    board as having no pads at all.
    """
    if design_data is None:
        return None
    components = getattr(design_data, "components", None)
    if not components:
        return None

    placed = [
        c for c in components
        if getattr(c, "placed", True) and getattr(c, "pads", None)
    ]
    if not placed:
        return None

    copper_layers = geometry.get_layers_by_type("copper")
    if not copper_layers:
        return None

    pad_polys: Dict[int, Tuple[str, Optional[str]]] = {}
    by_component: Dict[str, List[Polygon]] = {}

    for lyr in copper_layers:
        polys = [p for p in lyr.polygons if len(p.vertices) >= 3]
        if not polys:
            continue
        side = (getattr(lyr, "side", None) or "").lower()
        index = PolygonIndex.from_polygons(polys)

        for comp in placed:
            comp_side = (getattr(comp, "side", None) or "").lower()
            # A through-hole pad exists on every layer; an SMD pad only on its
            # component's own side. When the component's side is unknown we do
            # not guess -- matching on both sides would invent pads.
            for pad in comp.pads:
                if not getattr(pad, "through_hole", False) and comp_side and side and comp_side != side:
                    continue
                pb = Bounds(pad.x_mm, pad.y_mm, pad.x_mm, pad.y_mm)
                for pos in index.query_bbox(pb):
                    poly = polys[cast(int, pos)]
                    if _point_in_polygon(pad.x_mm, pad.y_mm, poly):
                        pad_polys[id(poly)] = (comp.ref, getattr(pad, "name", None))
                        by_component.setdefault(comp.ref, []).append(poly)

    if not pad_polys:
        return None
    return PadMap(pad_polys, by_component)


def get_or_build_pad_map(ctx) -> Optional[PadMap]:
    """Cached accessor: build once per run, shared by all pad-aware checks."""
    cache = getattr(ctx, "geometry_cache", None)
    design_data = getattr(ctx, "design_data", None)
    if cache is None:
        return build_pad_map(ctx.geometry, design_data)
    key = cache.key("pad_map")
    if cache.has(key):
        return cache.get(key)
    value = build_pad_map(ctx.geometry, design_data)
    cache.set(key, value)
    return value
