"""
Net-tagged geometry: correlate design-data nets with copper polygons.

Bare Gerber copper polygons carry no net identity. When a design-data source
(KiCad, IPC-2581, sidecar) provides *routed geometry* — per-net segments with a
layer — we can infer which copper polygon belongs to which net by testing which
net's centreline runs through each polygon. That unlocks the whole class of
"what other-net copper is near this net" / "what is under this trace" queries the
high-speed SI checks need, and lets any proximity check label findings by net.

Design:
  * ``build_net_map(geometry, design_data)`` returns a ``NetMap`` or ``None``
    (None whenever there is no design data or no routed geometry — net-aware
    checks then stay ``not_applicable``, the honest default).
  * Correlation is a broad-phase :class:`PolygonIndex` lookup (segment bbox ->
    candidate polygons) refined by sampling the segment centreline against each
    candidate polygon. Ambiguous polygons are resolved by majority vote.
  * Proximity queries reuse a per-layer :class:`PolygonIndex`, never O(n²).

This module lives under ``geometry`` and deliberately depends only on
``primitives``/``polygon_index`` (not on ``checks``), so the dependency
direction stays one-way.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Tuple, cast

from ..ingest.design_model import DesignData
from .layer_model import BoardGeometry, BoardLayer
from .polygon_index import PolygonIndex
from .primitives import Bounds, Polygon

# (logical_layer, polygon)
NetPolygon = Tuple[str, Polygon]
# (gap_mm, logical_layer, x_mm, y_mm)
EdgeGap = Tuple[float, str, float, float]


# --------------------------------------------------------------------------- #
# Small geometry helpers (self-contained; no dependency on checks/)
# --------------------------------------------------------------------------- #

def _poly_pts(poly: Polygon) -> List[Tuple[float, float]]:
    return [(float(v.x), float(v.y)) for v in poly.vertices]


def _point_in_polygon(x: float, y: float, pts: List[Tuple[float, float]]) -> bool:
    inside = False
    n = len(pts)
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[(i + 1) % n]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
    return inside


def _pt_to_segment(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    dx, dy = x2 - x1, y2 - y1
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _min_pt_to_edges(x: float, y: float, pts: List[Tuple[float, float]]) -> float:
    best = math.inf
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        d = _pt_to_segment(x, y, x1, y1, x2, y2)
        if d < best:
            best = d
    return best


def _min_poly_distance(a: Polygon, b: Polygon) -> float:
    """Approximate min boundary-to-boundary distance between two polygons
    (min over each polygon's vertices to the other's edges) -- the same
    convex-leaning estimate the mask checks use. 0 when they touch/overlap."""
    pa, pb = _poly_pts(a), _poly_pts(b)
    if len(pa) < 3 or len(pb) < 3:
        return math.inf
    best = math.inf
    for x, y in pa:
        d = _min_pt_to_edges(x, y, pb)
        if d < best:
            best = d
    for x, y in pb:
        d = _min_pt_to_edges(x, y, pa)
        if d < best:
            best = d
    return best


def _canon_layer(name: Optional[str]) -> Optional[str]:
    """Canonicalize a layer name from any source to top/bottom/innerN.

    Handles KiCad ("F.Cu", "B.Cu", "In1.Cu"), the engine's logical layers
    ("TopCopper", "BottomCopper", "InnerCopper1"), and common IPC-2581 refs.
    Falls back to the lowercased raw name so at least exact matches still line up.
    """
    if not name:
        return None
    s = name.strip().lower()
    if s.startswith("f.") or "top" in s:
        return "top"
    if s.startswith("b.") or "bottom" in s or s.startswith("bot"):
        return "bottom"
    m = (re.search(r"in(?:ner)?(?:copper)?\s*(\d+)", s)
         or re.search(r"in(\d+)\.cu", s)
         or re.search(r"\bl(\d+)\b", s))
    if m:
        return f"inner{int(m.group(1))}"
    return s


def _seg_hits_polygon(a: Tuple[float, float], b: Tuple[float, float], poly: Polygon,
                      samples: int = 5) -> bool:
    """True if the segment centreline a-b runs through ``poly`` (sampled)."""
    pts = _poly_pts(poly)
    if len(pts) < 3:
        return False
    for i in range(samples + 1):
        t = i / samples
        x = a[0] + t * (b[0] - a[0])
        y = a[1] + t * (b[1] - a[1])
        if _point_in_polygon(x, y, pts):
            return True
    return False


def _seg_bounds(a: Tuple[float, float], b: Tuple[float, float]) -> Bounds:
    return Bounds(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))


# --------------------------------------------------------------------------- #
# NetMap
# --------------------------------------------------------------------------- #

class NetMap:
    """Bidirectional net <-> copper-polygon association with proximity queries."""

    def __init__(self, net_to_polys: Dict[str, List[NetPolygon]],
                 poly_net: Dict[int, str]) -> None:
        self._net_to_polys = net_to_polys
        self._poly_net = poly_net
        # Lazily-built per-(net, layer) proximity indices.
        self._indices: Dict[Tuple[str, str], Tuple[PolygonIndex, List[Polygon]]] = {}

    # -- lookups ----------------------------------------------------------- #
    def nets(self) -> List[str]:
        return sorted(self._net_to_polys)

    def net_of(self, poly: Polygon) -> Optional[str]:
        return self._poly_net.get(id(poly))

    def polygons_for_net(self, net: str) -> List[NetPolygon]:
        return self._net_to_polys.get(net, [])

    def tagged_polygon_count(self) -> int:
        return len(self._poly_net)

    # -- proximity --------------------------------------------------------- #
    def _index(self, net: str, layer: str) -> Optional[Tuple[PolygonIndex, List[Polygon]]]:
        key = (net, layer)
        if key not in self._indices:
            polys = [p for (lyr, p) in self._net_to_polys.get(net, []) if lyr == layer]
            if not polys:
                return None
            self._indices[key] = (PolygonIndex.from_polygons(polys), polys)
        return self._indices[key]

    def coupled_edge_gaps(self, net_a: str, net_b: str,
                          max_gap_mm: float) -> List[EdgeGap]:
        """For each polygon of ``net_a``, the copper edge-to-edge gap to the
        nearest ``net_b`` polygon on the *same* layer, kept when within
        ``max_gap_mm`` (i.e. actually coupled). Returns (gap, layer, x, y)."""
        gaps: List[EdgeGap] = []
        for layer, poly_a in self._net_to_polys.get(net_a, []):
            idx = self._index(net_b, layer)
            if idx is None:
                continue
            index, polys_b = idx
            pb = poly_a.bounds()
            best = math.inf
            for pos in index.nearby(pb, max_gap_mm):
                d = _min_poly_distance(poly_a, polys_b[cast(int, pos)])
                if d < best:
                    best = d
            if best <= max_gap_mm:
                gaps.append((best, layer,
                             0.5 * (pb.min_x + pb.max_x), 0.5 * (pb.min_y + pb.max_y)))
        return gaps

    def min_spacing_between_nets(self, net_a: str, net_b: str,
                                 max_gap_mm: float = math.inf) -> Optional[float]:
        gaps = self.coupled_edge_gaps(net_a, net_b, max_gap_mm)
        return min((g[0] for g in gaps), default=None)


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #

def build_net_map(geometry: BoardGeometry,
                  design_data: Optional[DesignData]) -> Optional[NetMap]:
    """Correlate routed net geometry with copper polygons. None when there is
    nothing to correlate (no design data / no routed geometry / no copper)."""
    if design_data is None:
        return None
    netted = [(name, net) for name, net in design_data.nets.items() if net.has_geometry()]
    if not netted:
        return None
    copper_layers = geometry.get_layers_by_type("copper")
    if not copper_layers:
        return None

    layers_by_canon: Dict[Optional[str], List[BoardLayer]] = {}
    for lyr in copper_layers:
        layers_by_canon.setdefault(_canon_layer(lyr.logical_layer), []).append(lyr)

    # Reusable per-layer index of that layer's polygons.
    layer_index: Dict[int, Tuple[PolygonIndex, List[Polygon]]] = {}

    def _index_of(lyr: BoardLayer) -> Tuple[PolygonIndex, List[Polygon]]:
        key = id(lyr)
        if key not in layer_index:
            polys = [p for p in lyr.polygons if len(p.vertices) >= 3]
            layer_index[key] = (PolygonIndex.from_polygons(polys), polys)
        return layer_index[key]

    votes: Dict[int, Dict[str, int]] = {}
    poly_ref: Dict[int, NetPolygon] = {}

    for name, net in netted:
        for (a, b), seg_layer, _w in net.route_segments():
            canon = _canon_layer(seg_layer)
            # Prefer the matching layer; if the source layer can't be mapped to a
            # copper layer we have, fall back to matching against all of them.
            targets = layers_by_canon.get(canon) or copper_layers
            sb = _seg_bounds(a, b)
            for lyr in targets:
                index, polys = _index_of(lyr)
                for pos in index.query_bbox(sb):
                    poly = polys[cast(int, pos)]
                    if _seg_hits_polygon(a, b, poly):
                        pid = id(poly)
                        bucket = votes.setdefault(pid, {})
                        bucket[name] = bucket.get(name, 0) + 1
                        poly_ref[pid] = (lyr.logical_layer, poly)

    if not votes:
        return None

    net_to_polys: Dict[str, List[NetPolygon]] = {}
    poly_net: Dict[int, str] = {}
    for pid, counts in votes.items():
        winner = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
        poly_net[pid] = winner
        net_to_polys.setdefault(winner, []).append(poly_ref[pid])

    return NetMap(net_to_polys, poly_net)


def get_or_build_net_map(ctx) -> Optional[NetMap]:
    """Cached accessor: build once per run, shared by all net-aware checks."""
    cache = getattr(ctx, "geometry_cache", None)
    if cache is None:
        return build_net_map(ctx.geometry, ctx.design_data)
    key = cache.key("net_map")
    if cache.has(key):
        return cache.get(key)
    value = build_net_map(ctx.geometry, ctx.design_data)
    cache.set(key, value)
    return value
