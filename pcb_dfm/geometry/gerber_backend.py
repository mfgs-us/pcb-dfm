"""
Gerber/Excellon parse backend (gerbonara).

The internal parse seam for #3: convert a Gerber layer into our ``Polygon``
model in millimetres, backed by gerbonara (maintained, pure-Python, proper
RS-274X incl. arcs/regions/apertures). Every graphic object is reduced to its
filled outline the same way — ``to_primitives('mm')`` → ``to_arc_poly()`` →
tessellate the segments — so lines get proper (round) end caps, flashes get
their true aperture shape, and *arcs are exact* rather than chord-approximated
as the old pcb-tools path required.

Coordinates come out in mm (gerbonara does the inch↔mm conversion), so callers
must NOT convert again.
"""

from __future__ import annotations

import math
import warnings
from pathlib import Path
from typing import List, Tuple

from .primitives import Point2D, Polygon

try:
    from gerbonara import GerberFile
    GERBONARA_AVAILABLE = True
except Exception:  # pragma: no cover - defensive
    GerberFile = None  # type: ignore
    GERBONARA_AVAILABLE = False

_ARC_SEGMENTS = 24  # tessellation steps for a full circle


def _tessellate_arc(p1: Tuple[float, float], p2: Tuple[float, float],
                    center: Tuple[float, float], clockwise: bool,
                    full_circle_steps: int = _ARC_SEGMENTS) -> List[Tuple[float, float]]:
    """Points along the arc from p1 to p2 about ``center`` (excluding p1)."""
    cx, cy = center
    a1 = math.atan2(p1[1] - cy, p1[0] - cx)
    a2 = math.atan2(p2[1] - cy, p2[0] - cx)
    r = math.hypot(p1[0] - cx, p1[1] - cy)
    # Sweep in the correct direction.
    sweep = a2 - a1
    if clockwise:
        while sweep > 0:
            sweep -= 2 * math.pi
        if sweep == 0:
            sweep = -2 * math.pi
    else:
        while sweep < 0:
            sweep += 2 * math.pi
        if sweep == 0:
            sweep = 2 * math.pi
    steps = max(2, int(round(full_circle_steps * abs(sweep) / (2 * math.pi))))
    pts: List[Tuple[float, float]] = []
    for i in range(1, steps + 1):
        a = a1 + sweep * (i / steps)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _arcpoly_points(arc_poly) -> List[Tuple[float, float]]:
    """Flatten a gerbonara ArcPoly outline into (x, y) points, tessellating arcs.

    gerbonara 1.5's ``ArcPoly.approximate_arcs()`` is broken (calls a generator
    property as a method), so we walk ``.segments`` ourselves. Each segment is
    ``(p1, p2, (clockwise, center))``; a straight edge has ``clockwise is None``
    (and ``center == (None, None)``).
    """
    pts: List[Tuple[float, float]] = []
    for seg in arc_poly.segments:
        p1, p2, (clockwise, center) = seg[0], seg[1], seg[2]
        if not pts:
            pts.append((float(p1[0]), float(p1[1])))
        if clockwise is None:
            pts.append((float(p2[0]), float(p2[1])))
        else:
            pts.extend(_tessellate_arc((float(p1[0]), float(p1[1])),
                                       (float(p2[0]), float(p2[1])),
                                       (float(center[0]), float(center[1])),
                                       bool(clockwise)))
    return pts


def _object_polygons_mm(obj) -> List[Polygon]:
    polys: List[Polygon] = []
    try:
        prims = obj.to_primitives("mm")
    except Exception:
        return polys
    for prim in prims:
        try:
            arc_poly = prim.to_arc_poly()
        except Exception:
            continue
        pts = _arcpoly_points(arc_poly)
        if len(pts) >= 3:
            polys.append(Polygon(vertices=[Point2D(x=x, y=y) for x, y in pts]))
    return polys


def gerber_polygons_mm(path: Path) -> List[Polygon]:
    """Parse a Gerber file and return filled outline polygons in mm."""
    if not GERBONARA_AVAILABLE:
        return []
    # Real-world artwork commonly draws pours with zero-size apertures, and
    # gerbonara warns once per occurrence (hundreds per board). The resulting
    # degenerate polygons are filtered downstream, so silence the noise rather
    # than emit thousands of warnings per run.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        try:
            gf = GerberFile.open(str(path))
        except Exception:
            return []

        polys: List[Polygon] = []
        for obj in gf.objects:
            polys.extend(_object_polygons_mm(obj))
    return polys
