"""Routed-trace geometry helpers for the trace-shape review checks.

These work on the *routed segment* model (``Net.route_segments()`` -> segment,
layer, width) rather than the filled copper artwork, because trace *shape* --
bend angles, necking, meander legs -- is a property of the centreline, which a
Gerber (filled copper) does not carry. They are pure geometry, no I/O.
"""

from __future__ import annotations

import re
from collections import defaultdict
from math import acos, cos, degrees, hypot, radians
from typing import Dict, List, Optional, Tuple

from ..ingest.design_model import DesignData, Net

Point = Tuple[float, float]
Seg = Tuple[Point, Point]

# GENUINELY high-speed / shape-sensitive net names -- the ones where a 90-deg
# bend or a tight meander is a real discontinuity. Deliberately NARROW: CAN, XTAL,
# I2C, low-speed SPI &c. are not shape-sensitive, and flagging their corners would
# be folklore.
_HS_RE = re.compile(
    r"(^|[_/-])(usb|hdmi|lvds|rgmii|rmii|pcie|sata|ddr|dqs|mipi|dsi|csi|serdes|"
    r"usb_?d[pm]|d\+|d-|rxp|rxn|txp|txn)([_/0-9+-]|$)",
    re.I)


def is_hs_name(name: str) -> bool:
    return bool(_HS_RE.search(name or ""))


def si_relevant_nets(dd: DesignData) -> set:
    """Nets whose *shape* matters: declared diff-pairs, controlled-impedance
    specs, or high-speed by name."""
    nets: set = set()
    for dp in dd.diff_pairs:
        nets.add(dp.positive)
        nets.add(dp.negative)
    for spec in dd.controlled_impedance:
        nets.add(spec.name)
    for name in dd.nets:
        if is_hs_name(name):
            nets.add(name)
    return nets & set(dd.nets)


def seg_len(seg: Seg) -> float:
    (a, b) = seg
    return hypot(b[0] - a[0], b[1] - a[1])


def seg_dir(seg: Seg) -> Point:
    (a, b) = seg
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = hypot(dx, dy)
    return (dx / n, dy / n) if n else (0.0, 0.0)


def interior_angle(a: Point, b: Point, c: Point) -> float:
    """Interior angle at ``b`` for the path a-b-c, degrees (180 = straight)."""
    v1 = (a[0] - b[0], a[1] - b[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    n1, n2 = hypot(*v1), hypot(*v2)
    if n1 == 0 or n2 == 0:
        return 180.0
    d = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
    return degrees(acos(d))


def near_parallel(d1: Point, d2: Point, tol_deg: float) -> bool:
    return abs(d1[0] * d2[0] + d1[1] * d2[1]) >= cos(radians(tol_deg))


def _pt_seg_dist(p: Point, seg: Seg) -> float:
    (x1, y1), (x2, y2) = seg
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return hypot(p[0] - x1, p[1] - y1)
    t = max(0.0, min(1.0, ((p[0] - x1) * dx + (p[1] - y1) * dy) / (dx * dx + dy * dy)))
    return hypot(p[0] - (x1 + t * dx), p[1] - (y1 + t * dy))


def seg_seg_gap(s1: Seg, s2: Seg) -> float:
    """Approx min distance between two segments (endpoint-to-segment min)."""
    return min(_pt_seg_dist(s1[0], s2), _pt_seg_dist(s1[1], s2),
               _pt_seg_dist(s2[0], s1), _pt_seg_dist(s2[1], s1))


def parallel_overlap_offset(s1: Seg, s2: Seg) -> Optional[Tuple[float, float]]:
    """For two near-parallel segments, ``(perp_offset, overlap_len)`` where
    perp_offset is the lateral distance between their centrelines and overlap_len
    is how far they run alongside each other. None if ``s1`` is degenerate.

    This is what distinguishes real meander legs (offset > 0, they run alongside)
    from consecutive/collinear segments of one trace (offset ~ 0)."""
    (ax, ay), _ = s1
    d = seg_dir(s1)
    if d == (0.0, 0.0):
        return None
    nx, ny = -d[1], d[0]
    length = seg_len(s1)

    def proj(p: Point) -> float:
        return (p[0] - ax) * d[0] + (p[1] - ay) * d[1]

    def perp(p: Point) -> float:
        return abs((p[0] - ax) * nx + (p[1] - ay) * ny)

    t0, t1 = proj(s2[0]), proj(s2[1])
    lo, hi = min(t0, t1), max(t0, t1)
    overlap = min(hi, length) - max(lo, 0.0)
    offset = 0.5 * (perp(s2[0]) + perp(s2[1]))
    return offset, overlap


def segments_by_layer(net: Net) -> Dict[Optional[str], List[Tuple[Seg, Optional[float]]]]:
    out: Dict[Optional[str], List[Tuple[Seg, Optional[float]]]] = defaultdict(list)
    for (seg, layer, width) in net.route_segments():
        out[layer].append((seg, width))
    return out


def bend_angles(segments: List[Tuple[Seg, Optional[float]]], tol: float = 1e-3
                ) -> List[Tuple[Point, float]]:
    """(vertex, interior_angle) at every point where exactly two of these
    (same-layer) segments meet -- i.e. a bend, not an endpoint, T-junction, or
    via transition."""
    inc: Dict[Tuple[int, int], List[Point]] = defaultdict(list)
    pts: Dict[Tuple[int, int], Point] = {}
    for (seg, _w) in segments:
        a, b = seg
        ka = (round(a[0] / tol), round(a[1] / tol))
        kb = (round(b[0] / tol), round(b[1] / tol))
        pts[ka], pts[kb] = a, b
        inc[ka].append(b)
        inc[kb].append(a)
    bends = []
    for vk, neigh in inc.items():
        if len(neigh) != 2:
            continue
        bends.append((pts[vk], interior_angle(neigh[0], pts[vk], neigh[1])))
    return bends
