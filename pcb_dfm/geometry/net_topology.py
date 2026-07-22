"""
Per-net routing topology: turn a net's routed segments into a graph and measure
its stubs.

A high-speed net ideally routes as one trunk between its terminals; any extra
branch that tees off and dead-ends is a *stub* that reflects the signal. This
builds the net graph (segment endpoints merged by position — which implicitly
stitches layer transitions where the two layers' traces meet at a via) and
returns the longest stub.

Terminals we can trust are excluded from stub measurement: the trunk's two far
ends, and any dead end that sits on a **via** (the signal continues to another
layer there — that Z-axis stub is ``backdrill_stub_length``'s job, not this
one). What remains is planar dangling copper.

Consumes only ``design_model.Net`` (segments + vias); no BoardGeometry. Returns
``None`` when there is nothing measurable (no routed edges, or a component with a
loop, which has no well-defined trunk/stub decomposition).
"""

from __future__ import annotations

import heapq
import math
from typing import Dict, List, Optional, Set, Tuple

Node = Tuple[int, int]
Adj = Dict[Node, List[Tuple[Node, float]]]


def _key(x: float, y: float, snap: float) -> Node:
    return (round(x / snap), round(y / snap))


def build_adjacency(net, snap: float = 0.05) -> Adj:
    """Undirected weighted graph of a net's routed copper (nodes snapped to a
    ``snap``-mm grid so coincident endpoints — including via stitches — merge)."""
    adj: Adj = {}
    for (a, b), _layer, _w in net.route_segments():
        na, nb = _key(a[0], a[1], snap), _key(b[0], b[1], snap)
        if na == nb:
            continue
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        adj.setdefault(na, []).append((nb, length))
        adj.setdefault(nb, []).append((na, length))
    return adj


def _component(adj: Adj, start: Node, seen: Set[Node]) -> List[Node]:
    stack, comp = [start], []
    seen.add(start)
    while stack:
        u = stack.pop()
        comp.append(u)
        for v, _w in adj[u]:
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return comp


def _dijkstra(adj: Adj, sources) -> Dict[Node, float]:
    dist: Dict[Node, float] = {s: 0.0 for s in sources}
    pq: List[Tuple[float, Node]] = [(0.0, s) for s in dist]
    heapq.heapify(pq)
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, math.inf):
            continue
        for v, w in adj[u]:
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                heapq.heappush(pq, (nd, v))
    return dist


def _farthest(adj: Adj, start: Node) -> Node:
    dist = _dijkstra(adj, [start])
    return max(dist, key=lambda n: dist[n])


def _trunk_path(adj: Adj, u: Node, v: Node) -> Set[Node]:
    """Nodes on the shortest u->v path (the trunk), via parent pointers."""
    dist: Dict[Node, float] = {u: 0.0}
    parent: Dict[Node, Optional[Node]] = {u: None}
    pq: List[Tuple[float, Node]] = [(0.0, u)]
    while pq:
        d, x = heapq.heappop(pq)
        if d > dist.get(x, math.inf):
            continue
        for y, w in adj[x]:
            nd = d + w
            if nd < dist.get(y, math.inf):
                dist[y] = nd
                parent[y] = x
                heapq.heappush(pq, (nd, y))
    path: Set[Node] = set()
    n: Optional[Node] = v
    while n is not None:
        path.add(n)
        n = parent.get(n)
    return path


def max_stub_length_mm(net, snap: float = 0.05) -> Optional[float]:
    """Longest planar stub on the net (mm), or None if nothing is measurable."""
    adj = build_adjacency(net, snap)
    if not adj:
        return None

    via_nodes: Set[Node] = {_key(v.x_mm, v.y_mm, snap) for v in getattr(net, "vias", [])}

    seen: Set[Node] = set()
    best: Optional[float] = None
    for node in list(adj):
        if node in seen:
            continue
        comp = _component(adj, node, seen)
        if len(comp) < 2:
            continue
        edge_count = sum(len(adj[n]) for n in comp) // 2
        if edge_count > len(comp) - 1:
            continue  # has a loop -> no clean trunk/stub decomposition

        u = _farthest(adj, comp[0])
        v = _farthest(adj, u)
        trunk = _trunk_path(adj, u, v)
        # Trusted terminals = trunk nodes + any via on this component.
        terminals = trunk | (via_nodes & set(comp))
        dist = _dijkstra(adj, terminals)
        comp_stub = max((dist.get(n, 0.0) for n in comp), default=0.0)
        best = comp_stub if best is None else max(best, comp_stub)

    return best
