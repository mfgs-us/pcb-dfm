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
from math import floor
from typing import Dict, List, Optional, Sequence, Tuple, cast

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


_SEG_SAMPLE_STEP_MM = 0.1  # walk a routed segment's centreline every ~0.1 mm


def _seg_bounds(a: Tuple[float, float], b: Tuple[float, float]) -> Bounds:
    return Bounds(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))


_FILL_CELL_MM = 0.2


def _rasterize_fill(pts: List[Tuple[float, float]], cell: float = _FILL_CELL_MM
                    ) -> set:
    """Scanline-rasterise a (possibly self-winding, hole-bearing) fill outline
    into the set of grid cells it covers. Even-odd crossings exclude the
    clearances woven into the outline, so a point in a clearance is not covered.
    Built once per fill; each copper polygon is then an O(1) cell lookup."""
    n = len(pts)
    if n < 3:
        return set()
    ys = [p[1] for p in pts]
    cells: set = set()
    for iy in range(floor(min(ys) / cell), floor(max(ys) / cell) + 1):
        yc = (iy + 0.5) * cell
        xs = []
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            if (y1 <= yc) != (y2 <= yc):
                xs.append(x1 + (yc - y1) * (x2 - x1) / (y2 - y1))
        xs.sort()
        for k in range(0, len(xs) - 1, 2):
            for ix in range(floor(xs[k] / cell), floor(xs[k + 1] / cell) + 1):
                cells.add((ix, iy))
    return cells


# --------------------------------------------------------------------------- #
# NetMap
# --------------------------------------------------------------------------- #

class NetMap:
    """Bidirectional net <-> copper-polygon association with proximity queries."""

    def __init__(self, net_to_polys: Dict[str, List[NetPolygon]],
                 poly_net: Dict[int, str], total_copper: int = 0) -> None:
        self._net_to_polys = net_to_polys
        self._poly_net = poly_net
        self._total_copper = total_copper
        # Lazily-built per-(net, layer) proximity indices.
        self._indices: Dict[Tuple[str, str], Tuple[PolygonIndex, List[Polygon]]] = {}

    def coverage(self) -> float:
        """Fraction of copper polygons that carry a net label (0..1). The higher
        it is, the more net-aware checks can be definitive rather than advisory.
        NOTE: only meaningful after the netlist is registered to the board -- an
        un-registered netlist scores near zero because its points miss the copper."""
        return (self._total_copper and len(self._poly_net) / self._total_copper) or 0.0

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

_TOUCH_TOL_MM = 0.01  # boundaries within 10 um are one conductor (render-gap slack)


def _seg_seg_distance(a1: Tuple[float, float], a2: Tuple[float, float],
                      b1: Tuple[float, float], b2: Tuple[float, float]) -> float:
    """Minimum distance between two segments; 0.0 when they intersect."""
    def sub(p, q): return (p[0] - q[0], p[1] - q[1])
    def cross(p, q): return p[0] * q[1] - p[1] * q[0]
    def dot(p, q): return p[0] * q[0] + p[1] * q[1]

    r = sub(a2, a1)
    s = sub(b2, b1)
    denom = cross(r, s)
    qp = sub(b1, a1)
    if abs(denom) > 1e-12:
        t = cross(qp, s) / denom
        u = cross(qp, r) / denom
        if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
            return 0.0  # segments cross

    def pt_seg(p, s1, s2):
        e = sub(s2, s1)
        length2 = dot(e, e)
        tt = 0.0 if length2 == 0.0 else max(0.0, min(1.0, dot(sub(p, s1), e) / length2))
        cx, cy = s1[0] + e[0] * tt, s1[1] + e[1] * tt
        return math.hypot(p[0] - cx, p[1] - cy)

    return min(pt_seg(a1, b1, b2), pt_seg(a2, b1, b2),
               pt_seg(b1, a1, a2), pt_seg(b2, a1, a2))


_GRID_EDGE_THRESHOLD = 40   # polygons with more edges than this get an edge grid
_TOUCH_GRID_CELL_MM = 1.0

Edge = Tuple[Tuple[float, float], Tuple[float, float]]
EdgeGrid = Tuple[float, Dict[Tuple[int, int], List[Edge]]]


def _polygon_edges(poly: Polygon) -> List[Edge]:
    pts = _poly_pts(poly)
    n = len(pts)
    if n < 2:
        return []
    return [(pts[i], pts[(i + 1) % n]) for i in range(n)]


def _build_edge_grid(edges: List[Edge], cell: float = _TOUCH_GRID_CELL_MM) -> EdgeGrid:
    """Bucket each edge into the grid cells its bbox spans, so a query edge only
    tests edges that are spatially near it -- turns a pour's thousands of edges
    from a linear scan into a handful of lookups."""
    grid: Dict[Tuple[int, int], List[Edge]] = {}
    for (p1, p2) in edges:
        ix0 = floor(min(p1[0], p2[0]) / cell)
        ix1 = floor(max(p1[0], p2[0]) / cell)
        iy0 = floor(min(p1[1], p2[1]) / cell)
        iy1 = floor(max(p1[1], p2[1]) / cell)
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                grid.setdefault((ix, iy), []).append((p1, p2))
    return (cell, grid)


def _edges_vs_grid(edges: List[Edge], grid_t: EdgeGrid, tol: float) -> bool:
    cell, grid = grid_t
    get = grid.get
    for (s1, s2) in edges:
        for ix in range(floor((min(s1[0], s2[0]) - tol) / cell),
                        floor((max(s1[0], s2[0]) + tol) / cell) + 1):
            for iy in range(floor((min(s1[1], s2[1]) - tol) / cell),
                            floor((max(s1[1], s2[1]) + tol) / cell) + 1):
                bucket = get((ix, iy))
                if bucket:
                    for (b1, b2) in bucket:
                        if _seg_seg_distance(s1, s2, b1, b2) <= tol:
                            return True
    return False


def _edges_vs_edges(ea: List[Edge], eb: List[Edge], tol: float) -> bool:
    for (a1, a2) in ea:
        axmin = (a1[0] if a1[0] < a2[0] else a2[0]) - tol
        axmax = (a1[0] if a1[0] > a2[0] else a2[0]) + tol
        aymin = (a1[1] if a1[1] < a2[1] else a2[1]) - tol
        aymax = (a1[1] if a1[1] > a2[1] else a2[1]) + tol
        for (b1, b2) in eb:
            if ((b1[0] if b1[0] > b2[0] else b2[0]) < axmin
                    or (b1[0] if b1[0] < b2[0] else b2[0]) > axmax
                    or (b1[1] if b1[1] > b2[1] else b2[1]) < aymin
                    or (b1[1] if b1[1] < b2[1] else b2[1]) > aymax):
                continue
            if _seg_seg_distance(a1, a2, b1, b2) <= tol:
                return True
    return False


def _fast_touch(ea: List[Edge], ga: Optional[EdgeGrid],
                eb: List[Edge], gb: Optional[EdgeGrid], tol: float) -> bool:
    """Edge-intersection/abut test given precomputed edges and optional grids."""
    if not ea or not eb:
        return False
    if ga is not None:
        return _edges_vs_grid(eb, ga, tol)
    if gb is not None:
        return _edges_vs_grid(ea, gb, tol)
    return _edges_vs_edges(ea, eb, tol)


def _polygons_touch(a: Polygon, b: Polygon, tol: float = _TOUCH_TOL_MM) -> bool:
    """True when two copper polygons are ONE conductor: their boundaries
    intersect or come within ``tol``.

    Deliberately an EDGE test, not a vertex-in-polygon test. A ground pour is a
    single polygon whose outline snakes around the traces it clears; a trace in
    one of those clearances is geometrically *inside* the pour's outline while
    its copper is a clearance-width (or more) from any pour edge. Vertex-in-
    polygon read that as "touching" and merged the trace into the pour's net --
    collapsing a dozen nets into one blob that then had to be discarded. Two
    conductors are joined only where their copper actually MEETS. Large polygons
    are indexed by an edge grid so this stays fast on multi-thousand-vertex pours.

    Safe consequence: a small polygon fully *contained* in a larger one with no
    edge contact is not merged (the trace-in-clearance case); the rare same-net
    fully-enclosed feature is merely left unlabelled, never mislabelled.
    """
    ba, bb = a.bounds(), b.bounds()
    if (ba.min_x > bb.max_x + tol or bb.min_x > ba.max_x + tol
            or ba.min_y > bb.max_y + tol or bb.min_y > ba.max_y + tol):
        return False
    ea, eb = _polygon_edges(a), _polygon_edges(b)
    ga = _build_edge_grid(ea) if len(ea) >= _GRID_EDGE_THRESHOLD else None
    gb = _build_edge_grid(eb) if len(eb) >= _GRID_EDGE_THRESHOLD else None
    return _fast_touch(ea, ga, eb, gb, tol)


def _propagate_nets_through_connected_copper(
    copper_layers: List[BoardLayer],
    poly_net: Dict[int, str],
    poly_ref: Dict[int, NetPolygon],
    bridges: Sequence[Tuple[float, float]] = (),
) -> None:
    """Spread net labels from access points across connected copper.

    A netlist tags only the copper its access points land in -- the pads and
    vias -- which on a real board is a small minority. Everything else, the
    traces and pour fragments doing the actual routing, stays unlabelled, and an
    unlabelled polygon is useless to a net-aware check: it cannot be called
    same-net or foreign.

    Copper that physically touches is one conductor and therefore one net, so we
    union touching polygons. But a net is a THREE-DIMENSIONAL object: a
    bottom-layer trace routed away from a via belongs to the same net as the
    top-layer copper on the other side of it. Unioning only within each layer
    left bottom copper at 10.7% labelled against 98.7% on top, because the
    netlist's SMD access points are nearly all on the component side (#20).

    ``bridges`` are plated through-hole locations -- vias and THT pins. Each is
    one conductor spanning every layer it passes through, so the polygons it
    lands in are unioned across layers. Unplated holes must NOT be passed here:
    a mounting hole connects nothing.

    Groups holding two different labels are left alone: that means either the
    netlist disagrees with the artwork or the shapes only appear to touch, and
    guessing there would be worse than staying silent.
    """
    # One union-find over every copper polygon on the board, so a conductor can
    # span layers.
    per_layer: List[Tuple[BoardLayer, List[Polygon], int, PolygonIndex]] = []
    total = 0
    for lyr in copper_layers:
        polys = [p for p in lyr.polygons if len(p.vertices) >= 3]
        if not polys:
            continue
        per_layer.append((lyr, polys, total, PolygonIndex.from_polygons(polys)))
        total += len(polys)
    if total == 0:
        return

    # Precompute each polygon's edges once, and an edge grid for the big ones
    # (pours), so a pour's thousands of edges are indexed a single time and every
    # touch test against it is a handful of cell lookups, not a linear scan.
    all_edges: List[List[Edge]] = [[]] * total
    all_grid: List[Optional[EdgeGrid]] = [None] * total
    for _lyr, polys, off, _index in per_layer:
        for i, poly in enumerate(polys):
            edges = _polygon_edges(poly)
            all_edges[off + i] = edges
            if len(edges) >= _GRID_EDGE_THRESHOLD:
                all_grid[off + i] = _build_edge_grid(edges)

    parent = list(range(total))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # 1) Within each layer: touching copper is one conductor -- UNLESS both
    #    polygons are directly seeded to DIFFERENT nets. Two known-different-net
    #    copper shapes are never one conductor (either they merely render close --
    #    adjacent connector/switch pads a few um apart -- or it is a real short,
    #    and we must not silently merge either into one ambiguous blob). Refusing
    #    this edge is safe: it touches only seed<->seed pairs, never unseeded
    #    copper, so nothing is mislabelled.
    for _lyr, polys, off, index in per_layer:
        for i, poly in enumerate(polys):
            ni = poly_net.get(id(poly))
            gi = off + i
            ei, gridi, bi = all_edges[gi], all_grid[gi], poly.bounds()
            for pos in index.query_bbox(poly.bounds()):
                j = cast(int, pos)
                if j <= i:
                    continue
                other_poly = polys[j]
                nj = poly_net.get(id(other_poly))
                # Never merge two directly-seeded different nets (see below).
                if ni is not None and nj is not None and ni != nj:
                    continue
                bj = other_poly.bounds()
                if (bi.min_x > bj.max_x + _TOUCH_TOL_MM or bj.min_x > bi.max_x + _TOUCH_TOL_MM
                        or bi.min_y > bj.max_y + _TOUCH_TOL_MM or bj.min_y > bi.max_y + _TOUCH_TOL_MM):
                    continue
                gj = off + j
                if _fast_touch(ei, gridi, all_edges[gj], all_grid[gj], _TOUCH_TOL_MM):
                    union(gi, gj)

    # 2) Across layers: a plated through-hole ties together the copper it passes
    #    through on every layer.
    for (bx, by) in bridges:
        pb = Bounds(bx, by, bx, by)
        landed: List[int] = []
        for _lyr, polys, off, index in per_layer:
            for pos in index.query_bbox(pb):
                k = cast(int, pos)
                # A via in a plane antipad lands in the void, not the plane copper.
                if polys[k].contains_point(bx, by):
                    landed.append(off + k)
        for other in landed[1:]:
            union(landed[0], other)

    # 3) Label each conductor from whichever of its polygons the netlist tagged.
    poly_at: List[Tuple[BoardLayer, Polygon]] = []
    for lyr, polys, _off, _index in per_layer:
        poly_at.extend((lyr, p) for p in polys)

    groups: Dict[int, List[int]] = {}
    for i in range(total):
        groups.setdefault(find(i), []).append(i)

    for members in groups.values():
        labels = {
            poly_net[id(poly_at[i][1])] for i in members
            if id(poly_at[i][1]) in poly_net
        }
        if len(labels) != 1:
            continue  # unlabelled, or ambiguous -- leave as is
        net = labels.pop()
        for i in members:
            lyr, poly = poly_at[i]
            pid = id(poly)
            if pid not in poly_net:
                poly_net[pid] = net
                poly_ref[pid] = (lyr.logical_layer, poly)


def build_net_map(geometry: BoardGeometry,
                  design_data: Optional[DesignData],
                  plated_vias: Optional[Sequence[Tuple[float, float]]] = None) -> Optional[NetMap]:
    """Correlate routed net geometry with copper polygons. None when there is
    nothing to correlate (no design data / no routed geometry / no copper)."""
    if design_data is None:
        return None
    netted = [
        (name, net) for name, net in design_data.nets.items()
        if net.has_geometry() or net.has_points() or net.fill_regions
    ]
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

    # A NETLIST (IPC-D-356) supplies access POINTS rather than routed paths: the
    # location of each pad, pin and via together with its net. Any copper
    # polygon containing such a point is on that net -- a direct and very
    # reliable association, and often the only one available, since most CAD
    # tools export a netlist far more readily than full routed geometry.
    for name, net in netted:
        for pt in net.points:
            canon = _canon_layer(pt.layer)
            targets = layers_by_canon.get(canon) or copper_layers
            pb = Bounds(pt.x_mm, pt.y_mm, pt.x_mm, pt.y_mm)
            for lyr in targets:
                index, polys = _index_of(lyr)
                for pos in index.query_bbox(pb):
                    poly = polys[cast(int, pos)]
                    # contains_point is hole-aware: a point in a plane antipad is
                    # in the void, not the copper, so it does not seed the plane.
                    if poly.contains_point(pt.x_mm, pt.y_mm):
                        pid = id(poly)
                        bucket = votes.setdefault(pid, {})
                        bucket[name] = bucket.get(name, 0) + 1
                        poly_ref[pid] = (lyr.logical_layer, poly)

    # Routed segments: WALK the centreline at a fine step and seed the polygon at
    # each point, instead of testing each candidate polygon with a coarse 5-point
    # sample that steps over the short stroke polygons a trace is rendered as. A
    # point query is cheap, so this is both denser and faster.
    for name, net in netted:
        for (a, b), seg_layer, _w in net.route_segments():
            canon = _canon_layer(seg_layer)
            targets = layers_by_canon.get(canon) or copper_layers
            length = math.hypot(b[0] - a[0], b[1] - a[1])
            steps = max(5, int(length / _SEG_SAMPLE_STEP_MM))
            seeded: set = set()
            for k in range(steps + 1):
                t = k / steps
                x, y = a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])
                pb = Bounds(x, y, x, y)
                for lyr in targets:
                    index, polys = _index_of(lyr)
                    for pos in index.query_bbox(pb):
                        poly = polys[cast(int, pos)]
                        pid = id(poly)
                        if pid in seeded or not poly.contains_point(x, y):
                            continue
                        seeded.add(pid)
                        bucket = votes.setdefault(pid, {})
                        bucket[name] = bucket.get(name, 0) + 1
                        poly_ref[pid] = (lyr.logical_layer, poly)

    # Poured-copper fill outlines (KiCad filled zones): rasterise each once and
    # seed any copper whose centroid lands in a covered cell. The fill already
    # excludes clearances, so foreign pads in a pour's antipad are not seeded.
    for name, net in netted:
        for (fill_layer, fill_pts) in getattr(net, "fill_regions", ()):
            cells = _rasterize_fill(fill_pts)
            if not cells:
                continue
            canon = _canon_layer(fill_layer)
            targets = layers_by_canon.get(canon) or copper_layers
            fb = Bounds(min(x for x, _ in fill_pts), min(y for _, y in fill_pts),
                        max(x for x, _ in fill_pts), max(y for _, y in fill_pts))
            for lyr in targets:
                index, polys = _index_of(lyr)
                for pos in index.query_bbox(fb):
                    poly = polys[cast(int, pos)]
                    pb2 = poly.bounds()
                    ccx = 0.5 * (pb2.min_x + pb2.max_x)
                    ccy = 0.5 * (pb2.min_y + pb2.max_y)
                    if (floor(ccx / _FILL_CELL_MM), floor(ccy / _FILL_CELL_MM)) in cells:
                        pid = id(poly)
                        bucket = votes.setdefault(pid, {})
                        bucket[name] = bucket.get(name, 0) + 1
                        poly_ref[pid] = (lyr.logical_layer, poly)

    if not votes:
        return None

    poly_net: Dict[int, str] = {}
    for pid, counts in votes.items():
        winner = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
        poly_net[pid] = winner

    # Plated through-holes bridge layers. Prefer the board's own plated drills;
    # fall back to the netlist's through-hole access points when the caller has
    # not supplied them. Unplated holes are excluded by the caller -- a mounting
    # hole joins nothing.
    bridges: Sequence[Tuple[float, float]] = plated_vias if plated_vias is not None else [
        (pt.x_mm, pt.y_mm)
        for net in design_data.nets.values()
        for pt in getattr(net, "points", []) or []
        if getattr(pt, "kind", None) == "through"
    ]
    _propagate_nets_through_connected_copper(copper_layers, poly_net, poly_ref, bridges)

    net_to_polys: Dict[str, List[NetPolygon]] = {}
    for pid, net_name in poly_net.items():
        net_to_polys.setdefault(net_name, []).append(poly_ref[pid])

    total_copper = sum(
        1 for lyr in copper_layers for p in lyr.polygons if len(p.vertices) >= 3
    )
    return NetMap(net_to_polys, poly_net, total_copper=total_copper)


def _plated_vias_from_ingest(ctx) -> Optional[List[Tuple[float, float]]]:
    """Plated drill locations, which are what tie copper together across layers.

    Only PLATED holes conduct; an unplated mounting hole passes through the board
    without connecting anything, so bridging on one would merge unrelated nets.
    """
    ingest = getattr(ctx, "ingest", None)
    files = getattr(ingest, "files", None)
    if not files:
        return None
    from .gerber_backend import excellon_hits_mm
    out: List[Tuple[float, float]] = []
    for f in files:
        if getattr(f, "layer_type", None) != "drill":
            continue
        if getattr(f, "is_plated", None) is False:
            continue
        try:
            out.extend((h.x_mm, h.y_mm) for h in excellon_hits_mm(f.path))
        except Exception:
            continue
    return out or None


def get_or_build_net_map(ctx) -> Optional[NetMap]:
    """Cached accessor: build once per run, shared by all net-aware checks."""
    cache = getattr(ctx, "geometry_cache", None)
    vias = _plated_vias_from_ingest(ctx)
    if cache is None:
        return build_net_map(ctx.geometry, ctx.design_data, vias)
    key = cache.key("net_map")
    if cache.has(key):
        return cache.get(key)
    value = build_net_map(ctx.geometry, ctx.design_data, vias)
    cache.set(key, value)
    return value
